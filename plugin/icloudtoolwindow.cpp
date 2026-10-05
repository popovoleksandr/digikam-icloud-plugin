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

#include "icloudtoolwindow.h"

// Qt includes

#include <QCloseEvent>
#include <QGroupBox>
#include <QIcon>
#include <QLabel>
#include <QLocale>
#include <QMessageBox>
#include <QPushButton>
#include <QTimer>

// Local includes

#include "icloudengine.h"
#include "icloudsession.h"

using namespace Digikam;

namespace DigikamGenericICloudPhotosPlugin
{

QString humanSize(qint64 bytes)
{
    return QLocale().formattedDataSize(bytes, 1, QLocale::DataSizeTraditionalFormat);
}

// --------------------------------------------------------------------------

ICloudSettingsWidget::ICloudSettingsWidget(QWidget* const parent,
                                           DInfoInterface* const iface,
                                           const QString& toolName,
                                           const QString& subtitle)
    : WSSettingsWidget(parent, iface, toolName),
      m_subtitle(subtitle)
{
    updateLabels();
}

void ICloudSettingsWidget::updateLabels(const QString& name, const QString& /*url*/)
{
    getHeaderLbl()->setText(QStringLiteral("<h2>iCloud Photos</h2><p>%1</p>").arg(m_subtitle));

    if (!name.isEmpty())
    {
        getUserNameLabel()->setText(QStringLiteral("<b>%1</b>").arg(name.toHtmlEscaped()));
    }
}

// --------------------------------------------------------------------------

class Q_DECL_HIDDEN ICloudToolWindow::Private
{
public:

    Private() = default;

    DInfoInterface*        iface      = nullptr;
    ICloudEngine*          engine     = nullptr;
    ICloudSession*         session    = nullptr;
    ICloudSettingsWidget*  widget     = nullptr;
    QPointer<QMessageBox>  engineBox;
    bool                   busy       = false;
    bool                   engineDown = false;
    bool                   closing    = false;
};

ICloudToolWindow::ICloudToolWindow(DInfoInterface* const iface, QWidget* const parent, const QString& objName)
    : WSToolDialog(parent, objName),
      d(new Private)
{
    // digiKam always passes its interface; without one (tests, odd hosts) the
    // base implementation stands in, which knows no albums or selection.

    d->iface   = iface ? iface : new DInfoInterface(this);
    d->engine  = new ICloudEngine(this);
    d->session = new ICloudSession(d->engine, this);

    setAttribute(Qt::WA_DeleteOnClose);
    setWindowIcon(QIcon(QStringLiteral(":/icloudphotos/icloudphotos.svg")));
    setModal(false);

    connect(d->engine, &ICloudEngine::engineFailed,
            this, &ICloudToolWindow::slotEngineFailed);

    connect(d->session, &ICloudSession::signedIn,
            this, &ICloudToolWindow::slotSignedIn);

    connect(d->session, &ICloudSession::signedOut,
            this, &ICloudToolWindow::slotSignedOut);

    connect(d->session, &ICloudSession::failed,
            this, &ICloudToolWindow::slotSessionFailed);

    connect(this, &WSToolDialog::cancelClicked,
            this, &ICloudToolWindow::slotCancelClicked);

    // Once the window is up, so the sign-in dialog has a parent to center on.

    QTimer::singleShot(0, this, &ICloudToolWindow::slotStartSession);
}

ICloudToolWindow::~ICloudToolWindow()
{
    delete d;
}

ICloudEngine* ICloudToolWindow::engine() const
{
    return d->engine;
}

ICloudSession* ICloudToolWindow::session() const
{
    return d->session;
}

DInfoInterface* ICloudToolWindow::iface() const
{
    return d->iface;
}

bool ICloudToolWindow::isBusy() const
{
    return d->busy;
}

ICloudSettingsWidget* ICloudToolWindow::settingsWidget() const
{
    return d->widget;
}

void ICloudToolWindow::setSettingsWidget(ICloudSettingsWidget* const widget)
{
    d->widget = widget;
    setMainWidget(widget);

    widget->getAccountBox()->setTitle(QStringLiteral("Account"));
    widget->getChangeUserBtn()->setIcon(QIcon::fromTheme(QStringLiteral("system-users")));

    connect(widget->getChangeUserBtn(), &QPushButton::clicked,
            this, &ICloudToolWindow::slotAccountButton);

    updateAccountBox();
    readSettings();
}

void ICloudToolWindow::slotStartSession()
{
    if (d->widget)
    {
        d->widget->getUserNameLabel()->setText(QStringLiteral("<i>Checking…</i>"));
    }

    d->session->start();
}

void ICloudToolWindow::updateAccountBox()
{
    if (!d->widget)
    {
        return;
    }

    const bool signedIn = d->session->isSignedIn();

    if (d->engineDown)
    {
        d->widget->getUserNameLabel()->setText(QStringLiteral("<i>iCloud Photos engine unavailable</i>"));
        d->widget->getChangeUserBtn()->setText(QStringLiteral("Retry"));
    }
    else
    {
        d->widget->updateLabels(d->session->accountText());
        d->widget->getChangeUserBtn()->setText(signedIn ? QStringLiteral("Change Account")
                                                        : QStringLiteral("Sign In"));
    }

    d->widget->getChangeUserBtn()->setEnabled(!d->busy);
}

void ICloudToolWindow::slotAccountButton()
{
    if (d->engineDown)
    {
        d->engineDown = false;
        updateAccountBox();
        slotStartSession();
        return;
    }

    if (!d->session->isSignedIn())
    {
        d->session->signIn();
        return;
    }

    const auto answer = QMessageBox::question(this, QStringLiteral("Change Account"),
                            QStringLiteral("Sign out of iCloud on this computer?\n\n"
                                           "Signing in again may ask for a verification code."),
                            QMessageBox::Yes | QMessageBox::No, QMessageBox::No);

    if (answer == QMessageBox::Yes)
    {
        d->session->changeAccount();
    }
}

void ICloudToolWindow::slotSignedIn()
{
    d->engineDown = false;
    updateAccountBox();
    accountChanged(true);
}

void ICloudToolWindow::slotSignedOut()
{
    updateAccountBox();
    accountChanged(false);
}

void ICloudToolWindow::slotSessionFailed(const QJsonObject& error)
{
    updateAccountBox();
    accountChanged(false);
    reportError(error, QStringLiteral("Couldn't check your iCloud account"));
}

void ICloudToolWindow::slotEngineFailed(const QString& message)
{
    d->engineDown = true;

    if (d->busy)
    {
        setBusy(false);
    }

    updateAccountBox();
    accountChanged(false);

    if (!d->engineBox)
    {
        d->engineBox = showMessage(QMessageBox::Critical, QStringLiteral("iCloud Photos"), message);
    }
}

void ICloudToolWindow::setBusy(bool busy)
{
    d->busy = busy;

    startButton()->setEnabled(!busy);
    setRejectButtonMode(busy ? QDialogButtonBox::Cancel : QDialogButtonBox::Close);
    updateAccountBox();
}

void ICloudToolWindow::slotCancelClicked()
{
    if (d->busy)
    {
        d->engine->cancel();
        cancelRequested();
    }
}

void ICloudToolWindow::reportError(const QJsonObject& error, const QString& what)
{
    const QString kind    = error.value(QStringLiteral("kind")).toString();
    const QString message = error.value(QStringLiteral("message")).toString();

    if ((kind == QLatin1String("cancelled")) || (kind == QLatin1String("engine")))
    {
        return;
    }

    if (kind == QLatin1String("not_signed_in"))
    {
        d->session->signIn(message.isEmpty() ? QStringLiteral("Please sign in to iCloud again.") : message);
        return;
    }

    showMessage(QMessageBox::Warning, QStringLiteral("iCloud Photos"),
                message.isEmpty() ? what + QLatin1Char('.')
                                  : QStringLiteral("%1:\n\n%2").arg(what, message));
}

QMessageBox* ICloudToolWindow::showMessage(int icon, const QString& title, const QString& text,
                                           const QString& details)
{
    QMessageBox* const box = new QMessageBox(static_cast<QMessageBox::Icon>(icon), title, text,
                                             QMessageBox::Ok, this);
    box->setAttribute(Qt::WA_DeleteOnClose);

    if (!details.isEmpty())
    {
        box->setDetailedText(details);
    }

    box->open();

    return box;
}

bool ICloudToolWindow::confirmStop()
{
    const auto answer = QMessageBox::question(this, QStringLiteral("iCloud Photos"),
                            QStringLiteral("A transfer is still running. Stop it and close this window?"),
                            QMessageBox::Yes | QMessageBox::No, QMessageBox::No);

    if (answer != QMessageBox::Yes)
    {
        return false;
    }

    d->engine->cancel();

    return true;
}

bool ICloudToolWindow::okToClose()
{
    // Closing a QDialog can go through both reject() and closeEvent().

    if (d->closing)
    {
        return true;
    }

    if (d->busy && !confirmStop())
    {
        return false;
    }

    d->closing = true;
    writeSettings();

    return true;
}

void ICloudToolWindow::reject()
{
    if (okToClose())
    {
        WSToolDialog::reject();
    }
}

void ICloudToolWindow::closeEvent(QCloseEvent* e)
{
    if (okToClose())
    {
        e->accept();
    }
    else
    {
        e->ignore();
    }
}

} // namespace DigikamGenericICloudPhotosPlugin
