/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : digiKam's own plugin loader finds the plugin through
 *               DK_PLUGIN_PATH, as when trying a build uninstalled.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

// Qt includes

#include <QTest>

// digiKam includes

#include "dpluginloader.h"
#include "dplugingeneric.h"

// Local includes

#include "testutils.h"

using namespace Digikam;

namespace
{

const QString IID = QStringLiteral("org.kde.digikam.plugin.generic.ICloudPhotos");

} // namespace

class DigikamLoaderTest : public QObject
{
    Q_OBJECT

private Q_SLOTS:

    void initTestCase()
    {
        // An explicit DK_PLUGIN_PATH (to try other values by hand) wins.

        if (qEnvironmentVariableIsEmpty("DK_PLUGIN_PATH"))
        {
            qputenv("DK_PLUGIN_PATH", QFile::encodeName(QStringLiteral(PLUGIN_DIR)));
        }

        DPluginLoader::instance()->init();
    }

    void testFound()
    {
        const QList<DPlugin*> plugins = DPluginLoader::instance()->allPlugins();
        QStringList iids;

        for (DPlugin* const p : plugins)
        {
            iids << p->iid();
        }

        qInfo().noquote() << "DK_PLUGIN_PATH =" << qEnvironmentVariable("DK_PLUGIN_PATH")
                          << "->" << plugins.size() << "plugins; system OneDrive plugin loaded:"
                          << iids.contains(QStringLiteral("org.kde.digikam.plugin.generic.OneDrive"));

        QVERIFY2(iids.contains(IID), qPrintable(iids.join(QLatin1Char(' '))));
    }

    void testActions()
    {
        QObject parent;
        DPluginLoader::instance()->registerGenericPlugins(&parent);

        const QList<DPluginAction*> actions = DPluginLoader::instance()->pluginActions(IID, &parent);
        QStringList names;

        for (DPluginAction* const ac : actions)
        {
            names << ac->objectName();
        }

        names.sort();
        QCOMPARE(names, QStringList({ QStringLiteral("export_icloudphotos"), QStringLiteral("import_icloudphotos") }));

        QVERIFY(DPluginLoader::instance()->pluginAction(QStringLiteral("import_icloudphotos"), &parent));
        QVERIFY(DPluginLoader::instance()->pluginsActions(DPluginAction::GenericImport, &parent).size() >= 1);
    }

    void cleanupTestCase()
    {
        DPluginLoader::instance()->cleanUp();
    }
};

DKICLOUD_TEST_MAIN(DigikamLoaderTest)

#include "digikam_loader_test.moc"
