/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : "Import from iCloud Photos": downloads the photos of
 *               the chosen iCloud albums that are not here yet.
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

class ICloudImportWindow : public ICloudToolWindow
{
    Q_OBJECT

public:

    explicit ICloudImportWindow(Digikam::DInfoInterface* const iface, QWidget* const parent = nullptr);
    ~ICloudImportWindow() override;

    /// Album ids currently ticked.
    QStringList checkedAlbums() const;

    /// The albums listed, as shown (for tests).
    QStringList albumNames() const;
    int         albumCount() const;

Q_SIGNALS:

    /// The albums of the current library have been listed.
    void signalAlbumsLoaded();

    /// A sync ended; @p ok false when it failed or was cancelled.
    void signalSyncFinished(bool ok, const QJsonObject& resultOrError);

protected:

    void accountChanged(bool signedIn) override;
    void cancelRequested()             override;
    void readSettings()                override;
    void writeSettings()               override;

private Q_SLOTS:

    void slotStartSync();
    void slotReload();
    void slotLibraryActivated(int index);
    void slotAlbumItemChanged();

private:

    void loadLibraries();
    void loadAlbums();
    void handleSyncEvent(const QJsonObject& event);
    void registerFile(const QJsonObject& event);
    void writeICloudMetadata(const QString& path, const QJsonObject& asset);
    void syncFinished(bool ok, const QJsonObject& reply);
    void updateControls();
    void setStatus(const QString& text);
    QString currentLibrary() const;

private:

    class Private;
    Private* const d = nullptr;
};

} // namespace DigikamGenericICloudPhotosPlugin
