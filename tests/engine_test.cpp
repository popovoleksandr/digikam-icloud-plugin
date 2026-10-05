/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : ICloudEngine against the protocol stub, and its
 *               failure reports.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

// Qt includes

#include <QElapsedTimer>
#include <QJsonArray>
#include <QPointer>
#include <QSignalSpy>
#include <QTest>

// Local includes

#include "icloudengine.h"
#include "testutils.h"

using namespace DigikamGenericICloudPhotosPlugin;

class EngineTest : public QObject
{
    Q_OBJECT

private Q_SLOTS:

    void init()
    {
        qputenv("DKICLOUD_ENGINE_DIR", QFile::encodeName(QStringLiteral(STUB_ENGINE_DIR)));
        qunsetenv("DKICLOUD_PYTHON");
        qunsetenv("DKICLOUD_STUB_FILES");
    }

    void testHello()
    {
        ICloudEngine engine;
        QSignalSpy spy(&engine, &ICloudEngine::replied);

        const int id = engine.request(QStringLiteral("hello"));
        QVERIFY(spy.wait(10000));
        QCOMPARE(spy.at(0).at(0).toInt(), id);
        QVERIFY(spy.at(0).at(1).toBool());
        QCOMPARE(spy.at(0).at(2).value<QJsonObject>().value(QStringLiteral("protocol")).toInt(), 1);
        QVERIFY(engine.isRunning());
    }

    void testEventsComeBeforeTheReply()
    {
        QTemporaryDir dir;
        const QString path = dir.filePath(QStringLiteral("a.jpg"));
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly));
        file.close();
        qputenv("DKICLOUD_STUB_FILES", QStringLiteral("[{\"path\": \"%1\"}]").arg(path).toUtf8());

        ICloudEngine engine;
        QStringList seen;
        bool done = false;

        engine.call(QStringLiteral("sync"), QJsonObject{ { QStringLiteral("dest"), dir.path() } }, this,
                    [&](bool ok, const QJsonObject& result)
                    {
                        QVERIFY(ok);
                        QCOMPARE(result.value(QStringLiteral("downloaded")).toInt(), 1);
                        seen << QStringLiteral("reply");
                        done = true;
                    },
                    [&](const QJsonObject& event)
                    {
                        seen << event.value(QStringLiteral("event")).toString();
                    });

        QTRY_VERIFY_WITH_TIMEOUT(done, 10000);
        QCOMPARE(seen, QStringList({ QStringLiteral("phase"), QStringLiteral("total"), QStringLiteral("file"),
                                     QStringLiteral("progress"), QStringLiteral("error"), QStringLiteral("reply") }));
    }

    void testRequestsAreAnsweredInOrder()
    {
        ICloudEngine engine;
        QList<int> order;
        const int a = engine.call(QStringLiteral("status"), QJsonObject(), this,
                                  [&](bool, const QJsonObject&) { order << 1; });
        const int b = engine.call(QStringLiteral("albums"), QJsonObject(), this,
                                  [&](bool ok, const QJsonObject& r)
                                  {
                                      QVERIFY(ok);
                                      QCOMPARE(r.value(QStringLiteral("albums")).toArray().size(), 3);
                                      order << 2;
                                  });
        QVERIFY(a != b);
        QTRY_COMPARE_WITH_TIMEOUT(order, QList<int>({ 1, 2 }), 10000);
    }

    void testDeadContextIsNotCalled()
    {
        ICloudEngine engine;
        QSignalSpy spy(&engine, &ICloudEngine::replied);
        bool called = false;

        {
            QObject context;
            engine.call(QStringLiteral("hello"), QJsonObject(), &context,
                        [&](bool, const QJsonObject&) { called = true; });
        }

        QVERIFY(spy.wait(10000));
        QVERIFY(!called);
    }

    void testErrorReply()
    {
        ICloudEngine engine;
        QJsonObject error;
        bool done = false;

        engine.call(QStringLiteral("no_such_command"), QJsonObject(), this,
                    [&](bool ok, const QJsonObject& e) { QVERIFY(!ok); error = e; done = true; });

        QTRY_VERIFY_WITH_TIMEOUT(done, 10000);
        QCOMPARE(error.value(QStringLiteral("kind")).toString(), QStringLiteral("bad_request"));
    }

    void testMissingPython()
    {
        qputenv("DKICLOUD_PYTHON", "/nonexistent/python3");

        ICloudEngine engine;
        QSignalSpy failed(&engine, &ICloudEngine::engineFailed);
        QJsonObject error;
        bool done = false;

        engine.call(QStringLiteral("hello"), QJsonObject(), this,
                    [&](bool ok, const QJsonObject& e) { QVERIFY(!ok); error = e; done = true; });

        QTRY_VERIFY_WITH_TIMEOUT(done, 10000);
        QCOMPARE(error.value(QStringLiteral("kind")).toString(), QStringLiteral("engine"));
        QVERIFY(error.value(QStringLiteral("message")).toString().contains(QLatin1String("was not found")));
        QVERIFY(failed.size() >= 1);
    }

    void testMissingPackage()
    {
        qputenv("DKICLOUD_ENGINE_DIR", "/nonexistent");

        ICloudEngine engine;
        QSignalSpy failed(&engine, &ICloudEngine::engineFailed);
        engine.request(QStringLiteral("hello"));

        QVERIFY(failed.wait(10000));
        QVERIFY2(failed.at(0).at(0).toString().contains(QLatin1String("missing from")),
                 qPrintable(failed.at(0).at(0).toString()));
    }

    void testCrash()
    {
        ICloudEngine engine;
        QSignalSpy failed(&engine, &ICloudEngine::engineFailed);
        QJsonObject error;
        bool done = false;

        engine.call(QStringLiteral("crash"), QJsonObject(), this,
                    [&](bool ok, const QJsonObject& e) { QVERIFY(!ok); error = e; done = true; });

        QTRY_VERIFY_WITH_TIMEOUT(done, 10000);
        QCOMPARE(error.value(QStringLiteral("kind")).toString(), QStringLiteral("engine"));
        const QString message = error.value(QStringLiteral("message")).toString();
        QVERIFY2(message.contains(QLatin1String("exit code 3")) && message.contains(QLatin1String("stub crash")),
                 qPrintable(message));
        QCOMPARE(failed.size(), 1);

        // The next request starts a new engine.

        QSignalSpy replies(&engine, &ICloudEngine::replied);
        engine.request(QStringLiteral("hello"));
        QVERIFY(replies.wait(10000));
        QVERIFY(replies.at(0).at(1).toBool());
    }

    void testShutdownOnDestruction()
    {
        QPointer<ICloudEngine> engine = new ICloudEngine;
        QSignalSpy spy(engine.data(), &ICloudEngine::replied);
        engine->request(QStringLiteral("hello"));
        QVERIFY(spy.wait(10000));
        QVERIFY(engine->isRunning());

        QElapsedTimer timer;
        timer.start();
        delete engine;
        QVERIFY(timer.elapsed() < 2500);    // the stub exits on "shutdown"; no kill timeout
    }
};

DKICLOUD_TEST_MAIN(EngineTest)

#include "engine_test.moc"
