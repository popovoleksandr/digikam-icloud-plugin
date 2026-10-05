/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : a digiKam plugin to import from and export to iCloud Photos.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#include "icloudphotosplugin.h"

// Qt includes

#include <QIcon>

// Local includes

#include "icloudexportwindow.h"
#include "icloudimportwindow.h"

namespace DigikamGenericICloudPhotosPlugin
{

ICloudPhotosPlugin::ICloudPhotosPlugin(QObject* const parent)
    : DPluginGeneric(parent)
{
}

void ICloudPhotosPlugin::cleanUp()
{
    delete m_importDlg;
    delete m_exportDlg;
}

QString ICloudPhotosPlugin::name() const
{
    return QStringLiteral("iCloud Photos");
}

QString ICloudPhotosPlugin::iid() const
{
    return QLatin1String(DPLUGIN_IID);
}

QIcon ICloudPhotosPlugin::icon() const
{
    return QIcon(QStringLiteral(":/icloudphotos/icloudphotos.svg"));
}

QString ICloudPhotosPlugin::description() const
{
    return QStringLiteral("A tool to sync with iCloud Photos");
}

QString ICloudPhotosPlugin::details() const
{
    return QStringLiteral("<p>This tool downloads the photos and videos of your iCloud Photos albums "
                          "that are not in digiKam yet, and uploads items from digiKam to your "
                          "iCloud Photos library.</p>"
                          "<p>It signs in like icloud.com does (with two-factor authentication); "
                          "your password is never stored. Apple publishes no API for iCloud Photos, "
                          "so this uses the same private web service as icloud.com and may break "
                          "when Apple changes it.</p>"
                          "<p>Version %1</p>").arg(QStringLiteral(PLUGIN_VERSION));
}

QString ICloudPhotosPlugin::handbookSection() const
{
    return QLatin1String("post_processing");
}

QString ICloudPhotosPlugin::handbookChapter() const
{
    return QLatin1String("export_tools");
}

QList<DPluginAuthor> ICloudPhotosPlugin::authors() const
{
    return QList<DPluginAuthor>()
            << DPluginAuthor(QStringLiteral("Oleksandr Popov"),
                             QStringLiteral("anantaram dot rns at gmail dot com"),
                             QStringLiteral("(C) 2026"));
}

void ICloudPhotosPlugin::setup(QObject* const parent)
{
    DPluginAction* const im = new DPluginAction(parent);
    im->setIcon(icon());
    im->setText(QStringLiteral("Import from iCloud Photos…"));
    im->setObjectName(QLatin1String("import_icloudphotos"));
    im->setActionCategory(DPluginAction::GenericImport);

    connect(im, SIGNAL(triggered(bool)),
            this, SLOT(slotImport()));

    addAction(im);

    DPluginAction* const ex = new DPluginAction(parent);
    ex->setIcon(icon());
    ex->setText(QStringLiteral("Export to iCloud Photos…"));
    ex->setObjectName(QLatin1String("export_icloudphotos"));
    ex->setActionCategory(DPluginAction::GenericExport);

    connect(ex, SIGNAL(triggered(bool)),
            this, SLOT(slotExport()));

    addAction(ex);
}

void ICloudPhotosPlugin::slotImport()
{
    if (!reactivateToolDialog(m_importDlg))
    {
        delete m_importDlg;
        m_importDlg = new ICloudImportWindow(infoIface(sender()), nullptr);
        m_importDlg->setPlugin(this);
        m_importDlg->show();
    }
}

void ICloudPhotosPlugin::slotExport()
{
    if (!reactivateToolDialog(m_exportDlg))
    {
        delete m_exportDlg;
        m_exportDlg = new ICloudExportWindow(infoIface(sender()), nullptr);
        m_exportDlg->setPlugin(this);
        m_exportDlg->show();
    }
}

} // namespace DigikamGenericICloudPhotosPlugin
