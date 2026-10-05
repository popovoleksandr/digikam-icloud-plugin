/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : the import, export and sign-in windows against the
 *               protocol stub.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

// Qt includes

#include <QCheckBox>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLineEdit>
#include <QPointer>
#include <QPushButton>
#include <QSettings>
#include <QSignalSpy>
#include <QStackedWidget>
#include <QTest>

// digiKam includes

#include "ditemslist.h"
#include "dmetadata.h"

// Local includes

#include "icloudexportwindow.h"
#include "icloudimportwindow.h"
#include "icloudlogindialog.h"
#include "testutils.h"

using namespace Digikam;
using namespace DigikamGenericICloudPhotosPlugin;
using namespace TestUtils;

namespace
{

QString makeJpeg(const QString& path)
{
    QImage image(48, 32, QImage::Format_RGB32);
    image.fill(Qt::darkCyan);

    return image.save(path, "JPEG") ? path : QString();
}

QLineEdit* editWithEcho(QWidget* const dlg, QLineEdit::EchoMode mode, bool codeField = false)
{
    const auto edits = dlg->findChildren<QLineEdit*>();

    for (QLineEdit* const e : edits)
    {
        if ((e->echoMode() == mode) && (codeField == (e->placeholderText() == QLatin1String("Verification code"))))
        {
            return e;
        }
    }

    return nullptr;
}

} // namespace

class WindowsTest : public QObject
{
    Q_OBJECT

private Q_SLOTS:

    void initTestCase()
    {
        MetaEngine::initializeExiv2();
    }

    void init()
    {
        qputenv("DKICLOUD_ENGINE_DIR", QFile::encodeName(QStringLiteral(STUB_ENGINE_DIR)));
        qputenv("DKICLOUD_STUB_SIGNED_IN", "1");
        qunsetenv("DKICLOUD_STUB_FILES");
        qunsetenv("DKICLOUD_STUB_LIBRARIES");
    }

    void cleanup()
    {
        closeMessageBoxes();
    }

    void testImportSync()
    {
        QTemporaryDir dest;
        const QString photo = makeJpeg(dest.filePath(QStringLiteral("IMG_0001.JPG")));
        const QString live  = dest.filePath(QStringLiteral("IMG_0001.MOV"));
        QVERIFY(!photo.isEmpty());
        QFile mov(live);
        QVERIFY(mov.open(QIODevice::WriteOnly));
        mov.write("not really a movie");
        mov.close();

        const QJsonArray files
        {
            QJsonObject{ { QStringLiteral("path"),     photo },
                         { QStringLiteral("favorite"), true  },
                         { QStringLiteral("albums"),   QJsonArray{ QStringLiteral("Travel/Trip") } },
                         { QStringLiteral("caption"),  QStringLiteral("At the beach") } },
            QJsonObject{ { QStringLiteral("path"),     live  },
                         { QStringLiteral("favorite"), true  },
                         { QStringLiteral("albums"),   QJsonArray{ QStringLiteral("Travel/Trip") } },
                         { QStringLiteral("role"),     QStringLiteral("live") } },
        };
        qputenv("DKICLOUD_STUB_FILES", QJsonDocument(files).toJson(QJsonDocument::Compact));

        FakeIface iface;
        iface.dest = dest.path();

        QPointer<ICloudImportWindow> win = new ICloudImportWindow(&iface, nullptr);
        QSignalSpy loaded(win.data(), &ICloudImportWindow::signalAlbumsLoaded);
        win->show();

        QVERIFY(loaded.wait(10000));
        QCOMPARE(win->albumNames(), QStringList({ QStringLiteral("All Photos"), QStringLiteral("Favorites"),
                                                  QStringLiteral("Travel/Trip") }));
        QCOMPARE(win->checkedAlbums(), QStringList({ QStringLiteral("all") }));
        QTRY_VERIFY(win->startButton()->isEnabled());

        QSignalSpy finished(win.data(), &ICloudImportWindow::signalSyncFinished);
        win->startButton()->click();
        QVERIFY(win->isBusy());
        QVERIFY(finished.wait(10000));
        QVERIFY(finished.at(0).at(0).toBool());
        QVERIFY(!win->isBusy());

        // Both files were scanned into digiKam; only the photo got the rating.

        const QUrl photoUrl = QUrl::fromLocalFile(photo);
        const QUrl liveUrl  = QUrl::fromLocalFile(live);
        QCOMPARE(iface.scanned, QList<QUrl>({ photoUrl, liveUrl }));
        QVERIFY(iface.infos.contains(photoUrl));
        QCOMPARE(iface.infos[photoUrl].value(QStringLiteral("rating")).toInt(), 5);
        QVERIFY(!iface.infos.contains(liveUrl));

        // The iCloud album, favorite and caption are in the photo's metadata.

        DMetadata meta(photo);
        QStringList tags;
        meta.getItemTagsPath(tags);
        QVERIFY2(tags.contains(QStringLiteral("iCloud Photos/Travel/Trip")), qPrintable(tags.join(QLatin1Char(','))));
        QCOMPARE(meta.getItemRating(), 5);
        QCOMPARE(meta.getItemComments().value(QStringLiteral("x-default")).caption, QStringLiteral("At the beach"));

        const QStringList boxes = closeMessageBoxes();
        QCOMPARE(boxes.size(), 1);
        QVERIFY2(boxes.first().contains(QLatin1String("Downloaded 2 new files")), qPrintable(boxes.first()));
        QVERIFY2(boxes.first().contains(QLatin1String("1 file could not be downloaded")), qPrintable(boxes.first()));

        // The choices are remembered, then the window deletes itself on close.

        win->close();
        QTRY_VERIFY(win.isNull());

        QSettings settings(QStringLiteral("digikam-icloud-photos"), QStringLiteral("plugin"));
        QCOMPARE(settings.value(QStringLiteral("Import/Albums_private_PrimarySync")).toStringList(),
                 QStringList({ QStringLiteral("all") }));
        QCOMPARE(settings.value(QStringLiteral("Import/Layout")).toString(), QStringLiteral("date"));
    }

    void testImportNeedsDestination()
    {
        FakeIface iface;     // no destination album chosen

        QPointer<ICloudImportWindow> win = new ICloudImportWindow(&iface, nullptr);
        QSignalSpy loaded(win.data(), &ICloudImportWindow::signalAlbumsLoaded);
        win->show();
        QVERIFY(loaded.wait(10000));

        win->startButton()->click();
        QVERIFY(!win->isBusy());

        const QStringList boxes = closeMessageBoxes();
        QCOMPARE(boxes.size(), 1);
        QVERIFY(boxes.first().contains(QLatin1String("Download to")));

        win->close();
        QTRY_VERIFY(win.isNull());
    }

    void testImportWithoutIface()
    {
        // digiKam always passes one, but nothing must crash without it.

        QPointer<ICloudImportWindow> win = new ICloudImportWindow(nullptr, nullptr);
        QSignalSpy loaded(win.data(), &ICloudImportWindow::signalAlbumsLoaded);
        win->show();
        QVERIFY(loaded.wait(10000));
        win->close();
        QTRY_VERIFY(win.isNull());
    }

    void testSignInWithTwoFactor()
    {
        qputenv("DKICLOUD_STUB_SIGNED_IN", "0");

        FakeIface iface;
        QPointer<ICloudImportWindow> win = new ICloudImportWindow(&iface, nullptr);
        QSignalSpy loaded(win.data(), &ICloudImportWindow::signalAlbumsLoaded);
        win->show();

        ICloudLoginDialog* dlg = nullptr;
        QTRY_VERIFY_WITH_TIMEOUT((dlg = win->findChild<ICloudLoginDialog*>()) && dlg->isVisible(), 10000);
        QVERIFY(!win->startButton()->isEnabled());

        QLineEdit* const user     = editWithEcho(dlg, QLineEdit::Normal);
        QLineEdit* const password = editWithEcho(dlg, QLineEdit::Password);
        QLineEdit* const code     = editWithEcho(dlg, QLineEdit::Normal, true);
        QVERIFY(user && password && code);

        QDialogButtonBox* const buttons = dlg->findChild<QDialogButtonBox*>();
        QPushButton* const ok           = buttons->button(QDialogButtonBox::Ok);
        QVERIFY(!ok->isEnabled());

        // A wrong password is reported inline; the field is cleared.

        user->setText(QStringLiteral("user@example.com"));
        password->setText(QStringLiteral("wrong"));
        ok->click();
        QVERIFY(!user->isEnabled());                            // busy until the reply
        QTRY_VERIFY_WITH_TIMEOUT(user->isEnabled(), 10000);
        QVERIFY(password->text().isEmpty());

        bool errorShown = false;

        for (QLabel* const l : dlg->findChildren<QLabel*>())
        {
            errorShown |= (l->isVisible() && l->text().contains(QLatin1String("incorrect")));
        }

        QVERIFY(errorShown);

        // The right password leads to the code page; a wrong code is retried.

        password->setText(QStringLiteral("secret"));
        ok->click();
        QStackedWidget* const pages = dlg->findChild<QStackedWidget*>();
        QTRY_COMPARE_WITH_TIMEOUT(pages->currentIndex(), 1, 10000);
        QCOMPARE(ok->text(), QStringLiteral("Verify"));

        QTest::keyClicks(code, QStringLiteral("000000"));      // submits on the 6th digit
        QTRY_VERIFY_WITH_TIMEOUT(code->isEnabled() && code->text().isEmpty(), 10000);
        QVERIFY(dlg->isVisible());

        QTest::keyClicks(code, QStringLiteral("123456"));
        QVERIFY(loaded.wait(10000));
        QTRY_VERIFY(!win->findChild<ICloudLoginDialog*>());
        QCOMPARE(win->albumCount(), 3);

        win->close();
        QTRY_VERIFY(win.isNull());
    }

    void testExportUpload()
    {
        QTemporaryDir dir;
        const QString a = makeJpeg(dir.filePath(QStringLiteral("a.jpg")));
        const QString b = makeJpeg(dir.filePath(QStringLiteral("b-skip.jpg")));
        const QString c = makeJpeg(dir.filePath(QStringLiteral("c-fail.jpg")));
        QVERIFY(!a.isEmpty() && !b.isEmpty() && !c.isEmpty());

        FakeIface iface;
        iface.selection = { QUrl::fromLocalFile(a), QUrl::fromLocalFile(b), QUrl::fromLocalFile(c) };

        QPointer<ICloudExportWindow> win = new ICloudExportWindow(&iface, nullptr);
        win->show();

        DItemsList* const list = win->findChild<DItemsList*>();
        QVERIFY(list);
        QTRY_COMPARE(list->imageUrls().size(), 3);
        QTRY_VERIFY_WITH_TIMEOUT(win->startButton()->isEnabled(), 10000);

        QSignalSpy finished(win.data(), &ICloudExportWindow::signalUploadFinished);
        win->startButton()->click();
        QVERIFY(finished.wait(10000));
        QVERIFY(finished.at(0).at(0).toBool());

        const QJsonObject result = finished.at(0).at(1).value<QJsonObject>();
        QCOMPARE(result.value(QStringLiteral("uploaded")).toInt(), 1);
        QCOMPARE(result.value(QStringLiteral("skipped")).toInt(), 1);
        QCOMPARE(result.value(QStringLiteral("failed")).toInt(), 1);

        QCOMPARE(list->listView()->findItem(QUrl::fromLocalFile(a))->state(), DItemsListViewItem::Success);
        QCOMPARE(list->listView()->findItem(QUrl::fromLocalFile(b))->state(), DItemsListViewItem::Success);
        QCOMPARE(list->listView()->findItem(QUrl::fromLocalFile(c))->state(), DItemsListViewItem::Failed);

        const QStringList boxes = closeMessageBoxes();
        QCOMPARE(boxes.size(), 1);
        QVERIFY2(boxes.first().contains(QLatin1String("Uploaded 1 file")), qPrintable(boxes.first()));

        win->close();
        QTRY_VERIFY(win.isNull());
    }

    void testEngineMissing()
    {
        qputenv("DKICLOUD_ENGINE_DIR", "/nonexistent");

        FakeIface iface;
        QPointer<ICloudExportWindow> win = new ICloudExportWindow(&iface, nullptr);
        win->show();

        QTRY_VERIFY_WITH_TIMEOUT(messageBoxCount() > 0, 10000);
        QTest::qWait(300);             // a second report would arrive by now
        const QStringList boxes = closeMessageBoxes();
        QCOMPARE(boxes.size(), 1);     // reported once, not once per failed request
        QVERIFY2(boxes.first().contains(QLatin1String("missing from")), qPrintable(boxes.first()));
        QVERIFY(!win->startButton()->isEnabled());

        win->close();
        QTRY_VERIFY(win.isNull());
    }
};

DKICLOUD_TEST_MAIN(WindowsTest)

#include "windows_test.moc"
