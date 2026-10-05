/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : the signed-in iCloud account of a window: checks the
 *               saved session and asks the user to sign in if needed.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#pragma once

// Qt includes

#include <QJsonObject>
#include <QObject>
#include <QPointer>
#include <QString>

class QWidget;

namespace DigikamGenericICloudPhotosPlugin
{

class ICloudEngine;

class ICloudSession : public QObject
{
    Q_OBJECT

public:

    ICloudSession(ICloudEngine* const engine, QWidget* const window);
    ~ICloudSession() override = default;

    /// Checks the engine and the saved session; signs in if needed.
    void start();

    /// Signs out, then offers to sign in with another account.
    void changeAccount();

    /// Opens the sign-in dialog (after a "not_signed_in" error, say).
    void signIn(const QString& note = QString());

    bool    isSignedIn()  const;
    QString accountText() const;      ///< "Name (email)" or "Not signed in"

Q_SIGNALS:

    void signedIn();
    void signedOut();
    void failed(const QJsonObject& error);    ///< an error reply ({kind, message})

private:

    void checkStatus(bool signInIfNeeded);

private:

    ICloudEngine*     m_engine   = nullptr;
    QPointer<QWidget> m_window;
    bool              m_signedIn = false;
    bool              m_dialogOpen = false;
    QString           m_name;
    QString           m_email;
    QString           m_appleId;
};

} // namespace DigikamGenericICloudPhotosPlugin
