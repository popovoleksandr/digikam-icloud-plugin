/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : the built plugin loads like a digiKam generic plugin.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

// Qt includes

#include <QFile>
#include <QPluginLoader>
#include <QTest>

// digiKam includes

#include "dplugingeneric.h"

// Local includes

#include "testutils.h"

using namespace Digikam;

class PluginLoadTest : public QObject
{
    Q_OBJECT

private Q_SLOTS:

    void testFileName()
    {
        // digiKam only picks up Generic_*.so.

        QVERIFY(QFileInfo(QStringLiteral(PLUGIN_FILE)).fileName().startsWith(QLatin1String("Generic_")));
    }

    void testLoadAndActions()
    {
        QPluginLoader loader(QStringLiteral(PLUGIN_FILE));
        QObject* const instance = loader.instance();
        QVERIFY2(instance, qPrintable(loader.errorString()));

        DPluginGeneric* const plugin = qobject_cast<DPluginGeneric*>(instance);
        QVERIFY(plugin);
        QCOMPARE(plugin->iid(), QStringLiteral("org.kde.digikam.plugin.generic.ICloudPhotos"));
        QCOMPARE(plugin->ifaceIid(), QStringLiteral(DIGIKAM_DPLUGIN_GENERIC_IID));
        QVERIFY(!plugin->name().isEmpty());
        QVERIFY(!plugin->icon().isNull());
        QVERIFY(!plugin->authors().isEmpty());

        QObject parent;
        plugin->setup(&parent);

        const QList<DPluginAction*> actions = plugin->actions(&parent);
        QCOMPARE(actions.size(), 2);
        QCOMPARE(plugin->count(), 2);

        DPluginAction* const im = plugin->findActionByName(QStringLiteral("import_icloudphotos"), &parent);
        DPluginAction* const ex = plugin->findActionByName(QStringLiteral("export_icloudphotos"), &parent);
        QVERIFY(im);
        QVERIFY(ex);
        QCOMPARE(im->actionCategory(), DPluginAction::GenericImport);
        QCOMPARE(ex->actionCategory(), DPluginAction::GenericExport);
        QCOMPARE(im->actionType(),     DPluginAction::Generic);
        QVERIFY(im->text().contains(QLatin1String("iCloud Photos")));

        plugin->cleanUp();
    }
};

DKICLOUD_TEST_MAIN(PluginLoadTest)

#include "plugin_load_test.moc"
