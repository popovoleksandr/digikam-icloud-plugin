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

#include "icloudlogindialog.h"

// Qt includes

#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QIcon>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QRegularExpressionValidator>
#include <QStackedWidget>
#include <QVBoxLayout>

// Local includes

#include "icloudengine.h"

namespace DigikamGenericICloudPhotosPlugin
{

namespace
{

enum Page
{
    CredentialsPage = 0,
    CodePage
};

const QString DEVICE = QStringLiteral("device");

} // namespace

ICloudLoginDialog::ICloudLoginDialog(ICloudEngine* const engine, QWidget* const parent)
    : QDialog(parent),
      m_engine(engine)
{
    setWindowTitle(QStringLiteral("Sign in to iCloud"));
    setWindowIcon(QIcon(QStringLiteral(":/icloudphotos/icloudphotos.svg")));
    setModal(true);
    setMinimumWidth(420);

    QLabel* const header = new QLabel(this);
    header->setWordWrap(true);
    header->setText(QStringLiteral("<b>Sign in with your Apple Account</b><br>"
                                   "Your password is only used to sign in. It is never stored."));

    m_noteLbl = new QLabel(this);
    m_noteLbl->setWordWrap(true);
    m_noteLbl->hide();

    // -- Apple Account and password -----------------------------------------

    QWidget* const credentials = new QWidget(this);
    QFormLayout* const form    = new QFormLayout(credentials);
    form->setContentsMargins(QMargins());

    m_appleIdEdt = new QLineEdit(credentials);
    m_appleIdEdt->setPlaceholderText(QStringLiteral("Email or phone number"));
    m_appleIdEdt->setInputMethodHints(Qt::ImhEmailCharactersOnly | Qt::ImhNoAutoUppercase);

    m_passwordEdt = new QLineEdit(credentials);
    m_passwordEdt->setEchoMode(QLineEdit::Password);
    m_passwordEdt->setInputMethodHints(Qt::ImhSensitiveData | Qt::ImhNoPredictiveText);

    m_chinaChB = new QCheckBox(QStringLiteral("Account in mainland China (iCloud operated by GCBD)"), credentials);

    form->addRow(QStringLiteral("Apple Account:"), m_appleIdEdt);
    form->addRow(QStringLiteral("Password:"),      m_passwordEdt);
    form->addRow(QString(),                        m_chinaChB);

    // -- Verification code ---------------------------------------------------

    QWidget* const code         = new QWidget(this);
    QVBoxLayout* const codeLay  = new QVBoxLayout(code);
    codeLay->setContentsMargins(QMargins());

    m_codeLbl = new QLabel(code);
    m_codeLbl->setWordWrap(true);

    m_codeEdt = new QLineEdit(code);
    m_codeEdt->setPlaceholderText(QStringLiteral("Verification code"));
    m_codeEdt->setValidator(new QRegularExpressionValidator(QRegularExpression(QStringLiteral("\\d{0,8}")), m_codeEdt));
    m_codeEdt->setInputMethodHints(Qt::ImhDigitsOnly);
    QFont big = m_codeEdt->font();
    big.setPointSizeF(big.pointSizeF() * 1.5);
    big.setLetterSpacing(QFont::AbsoluteSpacing, 4.0);
    m_codeEdt->setFont(big);
    m_codeEdt->setAlignment(Qt::AlignCenter);

    QWidget* const deliveryRow = new QWidget(code);
    QHBoxLayout* const delLay  = new QHBoxLayout(deliveryRow);
    delLay->setContentsMargins(QMargins());
    m_deliveryCoB = new QComboBox(deliveryRow);
    m_sendCodeBtn = new QPushButton(QStringLiteral("Send New Code"), deliveryRow);
    delLay->addWidget(new QLabel(QStringLiteral("Send code to:"), deliveryRow));
    delLay->addWidget(m_deliveryCoB, 1);
    delLay->addWidget(m_sendCodeBtn);

    codeLay->addWidget(m_codeLbl);
    codeLay->addWidget(m_codeEdt);
    codeLay->addWidget(deliveryRow);

    m_pages = new QStackedWidget(this);
    m_pages->addWidget(credentials);
    m_pages->addWidget(code);

    // -- Messages and buttons ------------------------------------------------

    m_errorLbl = new QLabel(this);
    m_errorLbl->setWordWrap(true);
    m_errorLbl->setStyleSheet(QStringLiteral("color: #d32f2f;"));
    m_errorLbl->hide();

    m_statusLbl = new QLabel(this);
    m_statusLbl->setWordWrap(true);
    m_statusLbl->hide();

    m_buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, this);
    m_buttons->button(QDialogButtonBox::Ok)->setText(QStringLiteral("Sign In"));
    m_buttons->button(QDialogButtonBox::Ok)->setDefault(true);

    QVBoxLayout* const lay = new QVBoxLayout(this);
    lay->addWidget(header);
    lay->addWidget(m_noteLbl);
    lay->addSpacing(6);
    lay->addWidget(m_pages);
    lay->addWidget(m_errorLbl);
    lay->addWidget(m_statusLbl);
    lay->addStretch(1);
    lay->addWidget(m_buttons);

    connect(m_buttons, &QDialogButtonBox::accepted,
            this, &ICloudLoginDialog::slotSubmit);

    connect(m_buttons, &QDialogButtonBox::rejected,
            this, &QDialog::reject);

    connect(m_sendCodeBtn, &QPushButton::clicked,
            this, &ICloudLoginDialog::slotSendCode);

    connect(m_codeEdt, &QLineEdit::textEdited,
            this, &ICloudLoginDialog::slotCodeEdited);

    connect(m_appleIdEdt, &QLineEdit::textChanged,
            this, &ICloudLoginDialog::updateButtons);

    connect(m_passwordEdt, &QLineEdit::textChanged,
            this, &ICloudLoginDialog::updateButtons);

    connect(m_codeEdt, &QLineEdit::textChanged,
            this, &ICloudLoginDialog::updateButtons);

    updateButtons();
}

void ICloudLoginDialog::setAppleId(const QString& appleId)
{
    m_appleIdEdt->setText(appleId);

    if (!appleId.isEmpty())
    {
        m_passwordEdt->setFocus();
    }
}

void ICloudLoginDialog::setNote(const QString& note)
{
    m_noteLbl->setText(note);
    m_noteLbl->setVisible(!note.isEmpty());
}

void ICloudLoginDialog::updateButtons()
{
    bool ready = false;

    if (m_pages->currentIndex() == CredentialsPage)
    {
        ready = !m_appleIdEdt->text().trimmed().isEmpty() && !m_passwordEdt->text().isEmpty();
    }
    else
    {
        ready = !m_codeEdt->text().isEmpty();
    }

    m_buttons->button(QDialogButtonBox::Ok)->setEnabled(ready && !m_busy);
}

void ICloudLoginDialog::setBusy(bool busy, const QString& status)
{
    m_busy = busy;

    m_appleIdEdt->setEnabled(!busy);
    m_passwordEdt->setEnabled(!busy);
    m_chinaChB->setEnabled(!busy);
    m_codeEdt->setEnabled(!busy);
    m_deliveryCoB->setEnabled(!busy);
    m_sendCodeBtn->setEnabled(!busy);

    m_statusLbl->setText(status);
    m_statusLbl->setVisible(busy && !status.isEmpty());

    if (busy)
    {
        m_errorLbl->hide();
    }

    updateButtons();
}

void ICloudLoginDialog::showError(const QString& message)
{
    m_errorLbl->setText(message);
    m_errorLbl->setVisible(!message.isEmpty());
}

void ICloudLoginDialog::slotSubmit()
{
    if (m_busy)
    {
        return;
    }

    if (m_pages->currentIndex() == CredentialsPage)
    {
        signIn();
    }
    else
    {
        verify();
    }
}

void ICloudLoginDialog::signIn()
{
    const QString appleId  = m_appleIdEdt->text().trimmed();
    const QString password = m_passwordEdt->text();

    if (appleId.isEmpty() || password.isEmpty())
    {
        return;
    }

    // The password goes to the engine's pipe and nowhere else.

    m_passwordEdt->clear();
    setBusy(true, QStringLiteral("Signing in…"));

    m_engine->call(QStringLiteral("sign_in"),
                   QJsonObject{ { QStringLiteral("apple_id"),       appleId              },
                                { QStringLiteral("password"),       password             },
                                { QStringLiteral("china_mainland"), m_chinaChB->isChecked() } },
                   this,
                   [this](bool ok, const QJsonObject& reply) { handleReply(ok, reply); },
                   [this](const QJsonObject& event)
                   {
                       if (event.value(QStringLiteral("event")).toString() == QLatin1String("message"))
                       {
                           setBusy(true, event.value(QStringLiteral("text")).toString());
                       }
                   });
}

void ICloudLoginDialog::verify()
{
    const QString code = m_codeEdt->text().trimmed();

    if (code.isEmpty())
    {
        return;
    }

    setBusy(true, QStringLiteral("Verifying…"));

    m_engine->call(QStringLiteral("verify_code"),
                   QJsonObject{ { QStringLiteral("code"), code } },
                   this,
                   [this](bool ok, const QJsonObject& reply) { handleReply(ok, reply); },
                   [this](const QJsonObject& event)
                   {
                       if (event.value(QStringLiteral("event")).toString() == QLatin1String("message"))
                       {
                           setBusy(true, event.value(QStringLiteral("text")).toString());
                       }
                   });
}

void ICloudLoginDialog::slotSendCode()
{
    const QString target = m_deliveryCoB->currentData().toString();
    QJsonObject args;

    if (target == DEVICE)
    {
        args.insert(QStringLiteral("method"), DEVICE);
    }
    else
    {
        args.insert(QStringLiteral("method"),   QStringLiteral("sms"));
        args.insert(QStringLiteral("phone_id"), QJsonValue::fromVariant(m_deliveryCoB->currentData(Qt::UserRole + 1)));
    }

    setBusy(true, QStringLiteral("Sending a new code…"));

    m_engine->call(QStringLiteral("send_code"), args, this,
                   [this](bool ok, const QJsonObject& reply) { handleReply(ok, reply); });
}

void ICloudLoginDialog::slotCodeEdited(const QString& text)
{
    // Submit as soon as the whole code is typed, like Apple's own sign-in.

    if (!m_busy && (text.length() == m_codeLength))
    {
        verify();
    }
}

void ICloudLoginDialog::handleReply(bool ok, const QJsonObject& reply)
{
    setBusy(false);

    if (!ok)
    {
        const QString kind    = reply.value(QStringLiteral("kind")).toString();
        QString       message = reply.value(QStringLiteral("message")).toString();

        if (kind == QLatin1String("cancelled"))
        {
            return;
        }

        if (kind == QLatin1String("wrong_code"))
        {
            if (message.isEmpty())
            {
                message = QStringLiteral("That code is incorrect. Try again.");
            }

            m_codeEdt->clear();
            m_codeEdt->setFocus();
        }
        else if (m_pages->currentIndex() == CredentialsPage)
        {
            m_passwordEdt->setFocus();
        }

        if (message.isEmpty())
        {
            message = QStringLiteral("Sign-in failed.");
        }

        showError(message);
        return;
    }

    const QString status = reply.value(QStringLiteral("status")).toString();

    if (status == QLatin1String("2fa"))
    {
        showChallenge(reply);
        return;
    }

    // "ok", and the empty reply of sign-in steps that need nothing more.

    showError(QString());
    accept();
}

void ICloudLoginDialog::showChallenge(const QJsonObject& challenge)
{
    m_codeLength = qBound(4, challenge.value(QStringLiteral("length")).toInt(6), 8);

    const QString delivery = challenge.value(QStringLiteral("delivery")).toString();
    const QString sentTo   = challenge.value(QStringLiteral("sent_to")).toString();

    if ((delivery == QLatin1String("sms")) && !sentTo.isEmpty())
    {
        m_codeLbl->setText(QStringLiteral("Enter the %1-digit verification code sent to %2.")
                           .arg(m_codeLength).arg(sentTo.toHtmlEscaped()));
    }
    else
    {
        m_codeLbl->setText(QStringLiteral("Enter the %1-digit verification code shown on your "
                                          "iPhone, iPad or Mac.").arg(m_codeLength));
    }

    m_deliveryCoB->clear();
    m_deliveryCoB->addItem(QStringLiteral("My Apple devices"), DEVICE);

    const QJsonArray phones = challenge.value(QStringLiteral("phones")).toArray();

    for (const QJsonValue& value : phones)
    {
        const QJsonObject phone = value.toObject();
        m_deliveryCoB->addItem(QStringLiteral("%1 (text message)").arg(phone.value(QStringLiteral("number")).toString()),
                               QStringLiteral("sms"));
        m_deliveryCoB->setItemData(m_deliveryCoB->count() - 1,
                                   phone.value(QStringLiteral("id")).toVariant(),
                                   Qt::UserRole + 1);
    }

    // Preselect where the current code went.

    if (delivery == QLatin1String("sms"))
    {
        for (int i = 1 ; i < m_deliveryCoB->count() ; ++i)
        {
            if (sentTo.isEmpty() || m_deliveryCoB->itemText(i).startsWith(sentTo))
            {
                m_deliveryCoB->setCurrentIndex(i);
                break;
            }
        }
    }

    m_codeEdt->setMaxLength(m_codeLength);
    m_codeEdt->clear();

    m_pages->setCurrentIndex(CodePage);
    m_buttons->button(QDialogButtonBox::Ok)->setText(QStringLiteral("Verify"));
    showError(QString());
    updateButtons();
    m_codeEdt->setFocus();
}

} // namespace DigikamGenericICloudPhotosPlugin
