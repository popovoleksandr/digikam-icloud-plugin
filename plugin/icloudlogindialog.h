/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : sign-in dialog: Apple Account, password and the
 *               two-factor verification code.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#pragma once

// Qt includes

#include <QDialog>
#include <QJsonObject>
#include <QString>

class QCheckBox;
class QComboBox;
class QDialogButtonBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QStackedWidget;

namespace DigikamGenericICloudPhotosPlugin
{

class ICloudEngine;

class ICloudLoginDialog : public QDialog
{
    Q_OBJECT

public:

    ICloudLoginDialog(ICloudEngine* const engine, QWidget* const parent);
    ~ICloudLoginDialog() override = default;

    void setAppleId(const QString& appleId);

    /// A line shown above the form ("Your session expired", ...).
    void setNote(const QString& note);

private Q_SLOTS:

    void slotSubmit();
    void slotSendCode();
    void slotCodeEdited(const QString& text);

private:

    void signIn();
    void verify();
    void showChallenge(const QJsonObject& challenge);
    void handleReply(bool ok, const QJsonObject& reply);
    void setBusy(bool busy, const QString& status = QString());
    void showError(const QString& message);
    void updateButtons();

private:

    ICloudEngine*     m_engine      = nullptr;

    QStackedWidget*   m_pages       = nullptr;
    QLabel*           m_noteLbl     = nullptr;
    QLineEdit*        m_appleIdEdt  = nullptr;
    QLineEdit*        m_passwordEdt = nullptr;
    QCheckBox*        m_chinaChB    = nullptr;

    QLabel*           m_codeLbl     = nullptr;
    QLineEdit*        m_codeEdt     = nullptr;
    QComboBox*        m_deliveryCoB = nullptr;
    QPushButton*      m_sendCodeBtn = nullptr;

    QLabel*           m_errorLbl    = nullptr;
    QLabel*           m_statusLbl   = nullptr;
    QDialogButtonBox* m_buttons     = nullptr;

    bool              m_busy        = false;
    int               m_codeLength  = 6;
};

} // namespace DigikamGenericICloudPhotosPlugin
