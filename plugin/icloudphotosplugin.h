/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : a digiKam plugin to import from and export to iCloud Photos.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#pragma once

// Qt includes

#include <QPointer>

// digiKam includes

#include "dplugingeneric.h"

#define DPLUGIN_IID "org.kde.digikam.plugin.generic.ICloudPhotos"

using namespace Digikam;

namespace DigikamGenericICloudPhotosPlugin
{

class ICloudImportWindow;
class ICloudExportWindow;

class ICloudPhotosPlugin : public DPluginGeneric
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID DPLUGIN_IID)
    Q_INTERFACES(Digikam::DPluginGeneric)

public:

    explicit ICloudPhotosPlugin(QObject* const parent = nullptr);
    ~ICloudPhotosPlugin()                override = default;

    QString name()                 const override;
    QString iid()                  const override;
    QIcon   icon()                 const override;
    QString details()              const override;
    QString description()          const override;
    QList<DPluginAuthor> authors() const override;
    QString handbookSection()      const override;
    QString handbookChapter()      const override;

    void setup(QObject* const)           override;
    void cleanUp()                       override;

private Q_SLOTS:

    void slotImport();
    void slotExport();

private:

    QPointer<ICloudImportWindow> m_importDlg;
    QPointer<ICloudExportWindow> m_exportDlg;
};

} // namespace DigikamGenericICloudPhotosPlugin
