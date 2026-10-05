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

#include "icloudsession.h"

// Qt includes

#include <QJsonObject>
#include <QWidget>

// Local includes

#include "icloudengine.h"
#include "icloudlogindialog.h"
#include "icloudphotos_debug.h"

namespace DigikamGenericICloudPhotosPlugin
{

namespace
{

constexpr int PROTOCOL_VERSION = 1;

} // namespace

ICloudSession::ICloudSession(ICloudEngine* const engine, QWidget* const window)
    : QObject(window),
      m_engine(engine),
      m_window(window)
{
}

bool ICloudSession::isSignedIn() const
{
    return m_signedIn;
}

QString ICloudSession::accountText() const
{
    if (!m_signedIn)
    {
        return QStringLiteral("Not signed in");
    }

    if (m_name.isEmpty())
    {
        return m_email;
    }

    if (m_email.isEmpty())
    {
        return m_name;
    }

    return QStringLiteral("%1 (%2)").arg(m_name, m_email);
}

void ICloudSession::start()
{
    m_engine->call(QStringLiteral("hello"), QJsonObject(), this,
        [this](bool ok, const QJsonObject& reply)
        {
            if (!ok)
            {
                Q_EMIT failed(reply);
                return;
            }

            const int protocol = reply.value(QStringLiteral("protocol")).toInt();

            if (protocol != PROTOCOL_VERSION)
            {
                qCWarning(DKICLOUD_LOG) << "Engine speaks protocol" << protocol
                                        << "; the plugin expects" << PROTOCOL_VERSION;
            }

            checkStatus(true);
        }
    );
}

void ICloudSession::checkStatus(bool signInIfNeeded)
{
    m_engine->call(QStringLiteral("status"), QJsonObject(), this,
        [this, signInIfNeeded](bool ok, const QJsonObject& reply)
        {
            if (!ok)
            {
                Q_EMIT failed(reply);
                return;
            }

            m_appleId  = reply.value(QStringLiteral("apple_id")).toString();
            m_signedIn = reply.value(QStringLiteral("signed_in")).toBool();
            m_name     = reply.value(QStringLiteral("name")).toString();
            m_email    = reply.value(QStringLiteral("email")).toString();

            if (m_email.isEmpty())
            {
                m_email = m_appleId;
            }

            if (m_signedIn)
            {
                Q_EMIT signedIn();
                return;
            }

            Q_EMIT signedOut();

            if (signInIfNeeded)
            {
                const bool expired = reply.value(QStringLiteral("has_account")).toBool();
                signIn(expired ? QStringLiteral("Your iCloud session has expired. Please sign in again.")
                               : QString());
            }
        }
    );
}

void ICloudSession::signIn(const QString& note)
{
    if (m_dialogOpen || !m_window)
    {
        return;
    }

    m_dialogOpen = true;
    m_signedIn   = false;

    ICloudLoginDialog* const dlg = new ICloudLoginDialog(m_engine, m_window);
    dlg->setAttribute(Qt::WA_DeleteOnClose);
    dlg->setAppleId(m_appleId);
    dlg->setNote(note);

    connect(dlg, &QDialog::finished,
            this, [this](int result)
        {
            m_dialogOpen = false;

            if (result == QDialog::Accepted)
            {
                checkStatus(false);
            }
            else
            {
                Q_EMIT signedOut();
            }
        }
    );

    dlg->open();
}

void ICloudSession::changeAccount()
{
    m_engine->call(QStringLiteral("sign_out"), QJsonObject(), this,
        [this](bool ok, const QJsonObject& reply)
        {
            if (!ok && (reply.value(QStringLiteral("kind")).toString() == QLatin1String("engine")))
            {
                Q_EMIT failed(reply);
                return;
            }

            // Any other error still leaves us signed out locally.

            m_signedIn = false;
            m_name.clear();
            m_email.clear();
            m_appleId.clear();

            Q_EMIT signedOut();
            signIn();
        }
    );
}

} // namespace DigikamGenericICloudPhotosPlugin
