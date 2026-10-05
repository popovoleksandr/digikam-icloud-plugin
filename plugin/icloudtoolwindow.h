/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : what the import and export windows share: the engine,
 *               the account box, busy state and error reporting.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#pragma once

// Qt includes

#include <QJsonObject>
#include <QPointer>
#include <QString>

// digiKam includes

#include "dinfointerface.h"
#include "wssettingswidget.h"
#include "wstooldialog.h"

class QCloseEvent;
class QMessageBox;

namespace DigikamGenericICloudPhotosPlugin
{

class ICloudEngine;
class ICloudSession;

/// The WSSettingsWidget both windows use; only the header and account label differ.
class ICloudSettingsWidget : public Digikam::WSSettingsWidget
{
public:

    ICloudSettingsWidget(QWidget* const parent,
                         Digikam::DInfoInterface* const iface,
                         const QString& toolName,
                         const QString& subtitle);

    void updateLabels(const QString& name = QString(), const QString& url = QString()) override;

private:

    QString m_subtitle;
};

class ICloudToolWindow : public Digikam::WSToolDialog
{
    Q_OBJECT

public:

    ICloudToolWindow(Digikam::DInfoInterface* const iface, QWidget* const parent, const QString& objName);
    ~ICloudToolWindow() override;

    ICloudEngine*            engine()  const;
    ICloudSession*           session() const;
    Digikam::DInfoInterface* iface()   const;
    bool                     isBusy()  const;

protected:

    /// Call once from the subclass constructor, after building the widget.
    void setSettingsWidget(ICloudSettingsWidget* const widget);
    ICloudSettingsWidget* settingsWidget() const;

    /// Signed in or out (or the engine is unusable): enable what makes sense.
    virtual void accountChanged(bool signedIn) = 0;

    /// The running transfer was asked to stop.
    virtual void cancelRequested() {}

    virtual void readSettings()  {}
    virtual void writeSettings() {}

    /// Disables Start and turns Close into Cancel while a transfer runs.
    void setBusy(bool busy);

    /**
     * Reports an error reply to the user. "cancelled" is silent, "engine"
     * has been reported through engineFailed, "not_signed_in" opens the
     * sign-in dialog. @p what is prefixed ("Couldn't load your albums").
     */
    void reportError(const QJsonObject& error, const QString& what);

    /// A message box that does not block the event loop.
    QMessageBox* showMessage(int icon, const QString& title, const QString& text,
                             const QString& details = QString());

    void reject()                    override;
    void closeEvent(QCloseEvent* e)  override;

private Q_SLOTS:

    void slotStartSession();
    void slotAccountButton();
    void slotSignedIn();
    void slotSignedOut();
    void slotSessionFailed(const QJsonObject& error);
    void slotEngineFailed(const QString& message);
    void slotCancelClicked();

private:

    bool confirmStop();
    bool okToClose();
    void updateAccountBox();

private:

    class Private;
    Private* const d = nullptr;
};

/// "12.3 MB"
QString humanSize(qint64 bytes);

} // namespace DigikamGenericICloudPhotosPlugin
