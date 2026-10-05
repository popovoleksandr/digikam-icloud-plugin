/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : the windows with the real engine against the fake
 *               iCloud server (tests/fake_icloud.py).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

// C includes

#include <signal.h>
#include <sys/prctl.h>

// Qt includes

#include <QDialogButtonBox>
#include <QImage>
#include <QLineEdit>
#include <QPointer>
#include <QProcess>
#include <QPushButton>
#include <QSignalSpy>
#include <QTest>

// digiKam includes

#include "ditemslist.h"

// Local includes

#include "icloudexportwindow.h"
#include "icloudimportwindow.h"
#include "icloudlogindialog.h"
#include "testutils.h"

using namespace Digikam;
using namespace DigikamGenericICloudPhotosPlugin;
using namespace TestUtils;

class EndToEndTest : public QObject
{
    Q_OBJECT

private Q_SLOTS:

    void initTestCase()
    {
        MetaEngine::initializeExiv2();

        if (!QFile::exists(QStringLiteral(REAL_ENGINE_DIR "/dkicloudphotos/__main__.py")))
        {
            QSKIP("The engine is not there");
        }

        m_fake.setProgram(QStringLiteral("python3"));
        m_fake.setArguments({ QStringLiteral(FAKE_ICLOUD) });
        m_fake.setStandardErrorFile(QProcess::nullDevice());

        // Gone with the test, even if it crashes.

        m_fake.setChildProcessModifier([]() { ::prctl(PR_SET_PDEATHSIG, SIGKILL); });
        m_fake.start();
        QVERIFY(m_fake.waitForStarted(10000));
        QVERIFY(m_fake.waitForReadyRead(20000));

        const QString url = QString::fromUtf8(m_fake.readLine()).trimmed();
        QVERIFY2(url.startsWith(QLatin1String("http")), qPrintable(url));

        qputenv("DKICLOUD_TEST_SERVER", url.toUtf8());
        qputenv("DKICLOUD_ENGINE_DIR",  REAL_ENGINE_DIR);
        qunsetenv("DKICLOUD_PYTHON");
    }

    void cleanupTestCase()
    {
        closeMessageBoxes();
        m_fake.kill();
        m_fake.waitForFinished(5000);
    }

    void testSignInListAndSync()
    {
        QTemporaryDir dest;
        FakeIface iface;
        iface.dest = dest.path();

        QPointer<ICloudImportWindow> win = new ICloudImportWindow(&iface, nullptr);
        QSignalSpy loaded(win.data(), &ICloudImportWindow::signalAlbumsLoaded);
        win->show();

        // Not signed in yet: the sign-in dialog opens.

        ICloudLoginDialog* dlg = nullptr;
        QTRY_VERIFY_WITH_TIMEOUT((dlg = win->findChild<ICloudLoginDialog*>()) && dlg->isVisible(), 20000);

        QLineEdit* user     = nullptr;
        QLineEdit* password = nullptr;

        for (QLineEdit* const e : dlg->findChildren<QLineEdit*>())
        {
            if (e->echoMode() == QLineEdit::Password)
            {
                password = e;
            }
            else if (e->placeholderText().startsWith(QLatin1String("Email")))
            {
                user = e;
            }
        }

        QVERIFY(user && password);
        user->setText(QStringLiteral("user@example.com"));
        password->setText(QStringLiteral("correct horse battery staple"));
        dlg->findChild<QDialogButtonBox*>()->button(QDialogButtonBox::Ok)->click();

        QVERIFY2(loaded.wait(60000), qPrintable(closeMessageBoxes().join(QLatin1Char('\n'))));
        QTRY_VERIFY(!win->findChild<ICloudLoginDialog*>());

        const QStringList names = win->albumNames();
        qInfo() << "albums:" << names;
        QVERIFY(names.contains(QStringLiteral("All Photos")));
        QVERIFY(names.contains(QStringLiteral("Trip")));
        QCOMPARE(win->checkedAlbums(), QStringList({ QStringLiteral("all") }));

        QSignalSpy finished(win.data(), &ICloudImportWindow::signalSyncFinished);
        QTRY_VERIFY(win->startButton()->isEnabled());
        win->startButton()->click();
        QVERIFY(finished.wait(60000));
        QVERIFY2(finished.at(0).at(0).toBool(), qPrintable(QJsonDocument(finished.at(0).at(1).value<QJsonObject>()).toJson()));

        const QStringList files = filesBelow(dest.path());
        qInfo() << "downloaded:" << files << finished.at(0).at(1).value<QJsonObject>();

        QVERIFY(files.filter(QStringLiteral("IMG_0001.JPG")).size() == 1);
        QVERIFY(files.filter(QStringLiteral("IMG_0002.HEIC")).size() == 1);
        QVERIFY(files.filter(QStringLiteral("IMG_0002.MOV")).size() == 1);   // Live Photo video
        QCOMPARE(iface.scanned.size(), files.size());

        closeMessageBoxes();

        // A second sync finds nothing new.

        finished.clear();
        iface.scanned.clear();
        win->startButton()->click();
        QVERIFY(finished.wait(60000));
        QVERIFY(finished.at(0).at(0).toBool());
        QCOMPARE(finished.at(0).at(1).value<QJsonObject>().value(QStringLiteral("downloaded")).toInt(), 0);
        QVERIFY(iface.scanned.isEmpty());
        QCOMPARE(filesBelow(dest.path()), files);

        const QStringList boxes = closeMessageBoxes();
        QVERIFY(!boxes.isEmpty() && boxes.first().contains(QLatin1String("up to date")));

        win->close();
        QTRY_VERIFY(win.isNull());
    }

    void testUpload()
    {
        // Signed in by the previous test: the engine kept the session.

        QTemporaryDir dir;
        QImage image(40, 30, QImage::Format_RGB32);
        image.fill(Qt::darkMagenta);
        const QString photo = dir.filePath(QStringLiteral("upload-me.jpg"));
        QVERIFY(image.save(photo, "JPEG"));

        FakeIface iface;
        iface.selection = { QUrl::fromLocalFile(photo) };

        for (int round = 0 ; round < 2 ; ++round)
        {
            QPointer<ICloudExportWindow> win = new ICloudExportWindow(&iface, nullptr);
            win->show();
            QTRY_VERIFY_WITH_TIMEOUT(win->startButton()->isEnabled(), 20000);

            QSignalSpy finished(win.data(), &ICloudExportWindow::signalUploadFinished);
            win->startButton()->click();
            QVERIFY(finished.wait(60000));
            QVERIFY(finished.at(0).at(0).toBool());

            const QJsonObject result = finished.at(0).at(1).value<QJsonObject>();
            qInfo() << "upload round" << round << result;

            // Uploaded once, then recognised as already in iCloud.

            QCOMPARE(result.value(QStringLiteral("uploaded")).toInt(), (round == 0) ? 1 : 0);
            QCOMPARE(result.value(QStringLiteral("skipped")).toInt(),  (round == 0) ? 0 : 1);

            closeMessageBoxes();
            win->close();
            QTRY_VERIFY(win.isNull());
        }
    }

private:

    QProcess m_fake;
};

DKICLOUD_TEST_MAIN(EndToEndTest)

#include "e2e_test.moc"
