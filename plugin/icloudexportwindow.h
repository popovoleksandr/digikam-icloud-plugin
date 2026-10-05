/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : "Export to iCloud Photos": uploads the selected items
 *               to the iCloud Photos library.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#pragma once

// Qt includes

#include <QJsonObject>

// Local includes

#include "icloudtoolwindow.h"

namespace DigikamGenericICloudPhotosPlugin
{

class ICloudExportWindow : public ICloudToolWindow
{
    Q_OBJECT

public:

    explicit ICloudExportWindow(Digikam::DInfoInterface* const iface, QWidget* const parent = nullptr);
    ~ICloudExportWindow() override;

Q_SIGNALS:

    /// An upload ended; @p ok false when it failed or was cancelled.
    void signalUploadFinished(bool ok, const QJsonObject& resultOrError);

protected:

    void accountChanged(bool signedIn) override;
    void cancelRequested()             override;

private Q_SLOTS:

    void slotStartUpload();
    void slotImageListChanged();

private:

    void handleUploadEvent(const QJsonObject& event);
    void uploadFinished(bool ok, const QJsonObject& reply);
    void updateControls();
    void setStatus(const QString& text);

private:

    class Private;
    Private* const d = nullptr;
};

} // namespace DigikamGenericICloudPhotosPlugin
