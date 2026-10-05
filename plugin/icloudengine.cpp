/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : the Python engine process that talks to iCloud,
 *               driven over JSON Lines (see docs/PROTOCOL.md).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#include "icloudengine.h"

// Qt includes

#include <QDir>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QProcessEnvironment>
#include <QStringList>
#include <QTimer>

// Local includes

#include "icloudphotos_debug.h"

Q_LOGGING_CATEGORY(DKICLOUD_LOG, "digikam.dplugin.generic.icloudphotos", QtWarningMsg)

namespace DigikamGenericICloudPhotosPlugin
{

namespace
{

constexpr int STDERR_KEEP = 8192;   // bytes of engine stderr kept for error messages

QJsonObject engineError(const QString& kind, const QString& message)
{
    return QJsonObject{ { QStringLiteral("kind"),    kind    },
                        { QStringLiteral("message"), message } };
}

} // namespace

ICloudEngine::ICloudEngine(QObject* const parent)
    : QObject(parent)
{
}

ICloudEngine::~ICloudEngine()
{
    // The windows owning the handlers are going away: no callbacks any more.

    m_pending.clear();

    if (m_proc)
    {
        m_proc->disconnect(this);
        shutdown();
    }
}

QString ICloudEngine::python()
{
    const QString env = qEnvironmentVariable("DKICLOUD_PYTHON");

    return env.isEmpty() ? QStringLiteral("python3") : env;
}

QString ICloudEngine::engineDir()
{
    const QString env = qEnvironmentVariable("DKICLOUD_ENGINE_DIR");

    return env.isEmpty() ? QStringLiteral(ENGINE_DIR) : env;
}

bool ICloudEngine::isRunning() const
{
    return (m_proc && (m_proc->state() != QProcess::NotRunning));
}

bool ICloudEngine::start()
{
    if (isRunning())
    {
        return true;
    }

    m_shuttingDown = false;
    m_stdout.clear();
    m_stderr.clear();

    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    const QString pythonPath = env.value(QStringLiteral("PYTHONPATH"));
    env.insert(QStringLiteral("PYTHONPATH"),
               pythonPath.isEmpty() ? engineDir()
                                    : engineDir() + QDir::listSeparator() + pythonPath);
    env.insert(QStringLiteral("PYTHONUNBUFFERED"), QStringLiteral("1"));
    env.insert(QStringLiteral("PYTHONIOENCODING"), QStringLiteral("utf-8"));

    m_proc = new QProcess(this);
    m_proc->setProcessEnvironment(env);
    m_proc->setProgram(python());
    m_proc->setArguments({ QStringLiteral("-m"), QStringLiteral("dkicloudphotos"), QStringLiteral("serve") });

    connect(m_proc, &QProcess::readyReadStandardOutput,
            this, &ICloudEngine::slotReadStdout);

    connect(m_proc, &QProcess::readyReadStandardError,
            this, &ICloudEngine::slotReadStderr);

    connect(m_proc, &QProcess::errorOccurred,
            this, &ICloudEngine::slotErrorOccurred);

    connect(m_proc, &QProcess::finished,
            this, &ICloudEngine::slotFinished);

    qCDebug(DKICLOUD_LOG) << "Starting engine:" << python() << m_proc->arguments()
                          << "PYTHONPATH=" << env.value(QStringLiteral("PYTHONPATH"));

    m_proc->start();

    if (!m_proc->waitForStarted(10000))
    {
        // slotErrorOccurred() has reported it.

        if (m_proc)
        {
            m_proc->disconnect(this);
            m_proc->deleteLater();
            m_proc = nullptr;
        }

        return false;
    }

    return true;
}

int ICloudEngine::request(const QString& cmd, const QJsonObject& args)
{
    const int id = m_nextId++;

    if (!start())
    {
        m_pending.insert(id, Handlers());
        const QString message = explainFailure(QString(), -1, true);

        // Fail it from the event loop, so callers always get the reply after request() returned.

        QTimer::singleShot(0, this, [this, id, message]()
            {
                if (m_pending.contains(id))
                {
                    deliverReply(id, false, engineError(QStringLiteral("engine"), message));
                }
            }
        );

        return id;
    }

    m_pending.insert(id, Handlers());

    QJsonObject msg{ { QStringLiteral("id"),  id  },
                     { QStringLiteral("cmd"), cmd } };

    if (!args.isEmpty())
    {
        msg.insert(QStringLiteral("args"), args);
    }

    m_proc->write(QJsonDocument(msg).toJson(QJsonDocument::Compact) + '\n');

    return id;
}

int ICloudEngine::call(const QString& cmd,
                       const QJsonObject& args,
                       QObject* const context,
                       const ReplyHandler& onReply,
                       const EventHandler& onEvent)
{
    Handlers handlers;
    handlers.context    = context;
    handlers.hasContext = (context != nullptr);
    handlers.onReply    = onReply;
    handlers.onEvent    = onEvent;

    // Replies (and start failures) are only delivered from the event loop,
    // so attaching the handlers after sending is in time.

    const int id = request(cmd, args);
    m_pending[id] = handlers;

    return id;
}

void ICloudEngine::cancel()
{
    if (!isRunning() || m_pending.isEmpty())
    {
        return;
    }

    const QJsonObject msg{ { QStringLiteral("id"),  m_nextId++               },
                           { QStringLiteral("cmd"), QStringLiteral("cancel") } };

    m_proc->write(QJsonDocument(msg).toJson(QJsonDocument::Compact) + '\n');
}

void ICloudEngine::shutdown()
{
    if (!m_proc)
    {
        return;
    }

    m_shuttingDown = true;

    if (m_proc->state() != QProcess::NotRunning)
    {
        const QJsonObject msg{ { QStringLiteral("id"),  m_nextId++                 },
                               { QStringLiteral("cmd"), QStringLiteral("shutdown") } };

        m_proc->write(QJsonDocument(msg).toJson(QJsonDocument::Compact) + '\n');
        m_proc->closeWriteChannel();

        if (!m_proc->waitForFinished(3000))
        {
            qCWarning(DKICLOUD_LOG) << "The iCloud Photos engine did not exit; killing it";
            m_proc->kill();
            m_proc->waitForFinished(2000);
        }
    }

    // When called from the destructor the handlers are gone already.

    failPending(QString());

    if (m_proc)
    {
        m_proc->disconnect(this);
        m_proc->deleteLater();
        m_proc = nullptr;
    }
}

void ICloudEngine::slotReadStdout()
{
    if (!m_proc)
    {
        return;
    }

    m_stdout += m_proc->readAllStandardOutput();

    QPointer<ICloudEngine> self(this);
    int nl = -1;

    while (self && ((nl = m_stdout.indexOf('\n')) >= 0))
    {
        const QByteArray line = m_stdout.left(nl).trimmed();
        m_stdout.remove(0, nl + 1);

        if (!line.isEmpty())
        {
            handleLine(line);
        }
    }
}

void ICloudEngine::slotReadStderr()
{
    if (!m_proc)
    {
        return;
    }

    const QByteArray data = m_proc->readAllStandardError();

    for (const QByteArray& line : data.split('\n'))
    {
        if (!line.trimmed().isEmpty())
        {
            qCDebug(DKICLOUD_LOG) << "engine:" << line.constData();
        }
    }

    m_stderr += data;

    if (m_stderr.size() > STDERR_KEEP)
    {
        m_stderr = m_stderr.right(STDERR_KEEP);
    }
}

void ICloudEngine::handleLine(const QByteArray& line)
{
    QJsonParseError err;
    const QJsonDocument doc = QJsonDocument::fromJson(line, &err);

    if ((err.error != QJsonParseError::NoError) || !doc.isObject())
    {
        qCWarning(DKICLOUD_LOG) << "Unreadable line from the engine:" << line.left(200);
        return;
    }

    const QJsonObject msg = doc.object();
    const int id          = msg.value(QStringLiteral("id")).toInt();   // null -> 0

    if (msg.contains(QStringLiteral("event")))
    {
        QPointer<ICloudEngine> self(this);
        Q_EMIT eventReceived(id, msg);

        if (!self)
        {
            return;
        }

        const auto it = m_pending.constFind(id);

        if ((it != m_pending.constEnd()) && it->onEvent && (!it->hasContext || it->context))
        {
            const EventHandler onEvent = it->onEvent;   // the handler may change m_pending
            onEvent(msg);
        }

        return;
    }

    if (msg.contains(QStringLiteral("ok")))
    {
        const bool ok = msg.value(QStringLiteral("ok")).toBool();

        deliverReply(id, ok, ok ? msg.value(QStringLiteral("result")).toObject()
                                : msg.value(QStringLiteral("error")).toObject());
        return;
    }

    qCWarning(DKICLOUD_LOG) << "Unexpected message from the engine:" << line.left(200);
}

void ICloudEngine::deliverReply(int id, bool ok, const QJsonObject& data)
{
    const Handlers handlers = m_pending.take(id);

    QPointer<ICloudEngine> self(this);
    Q_EMIT replied(id, ok, data);

    if (!self)
    {
        return;
    }

    if (handlers.onReply && (!handlers.hasContext || handlers.context))
    {
        handlers.onReply(ok, data);
    }
}

void ICloudEngine::failPending(const QString& message)
{
    const QList<int> ids = m_pending.keys();

    for (int id : ids)
    {
        if (!m_pending.contains(id))
        {
            continue;
        }

        if (message.isEmpty())
        {
            deliverReply(id, false, engineError(QStringLiteral("cancelled"),
                                                QStringLiteral("The iCloud Photos engine was stopped.")));
        }
        else
        {
            deliverReply(id, false, engineError(QStringLiteral("engine"), message));
        }
    }
}

void ICloudEngine::slotErrorOccurred(QProcess::ProcessError error)
{
    if (error != QProcess::FailedToStart)
    {
        // Crashes are handled in slotFinished(); write errors show up there too.

        return;
    }

    const QString message = explainFailure(QString::fromUtf8(m_stderr), -1, true);
    qCWarning(DKICLOUD_LOG) << message;

    failPending(message);
    Q_EMIT engineFailed(message);
}

void ICloudEngine::slotFinished(int exitCode, QProcess::ExitStatus status)
{
    if (!m_proc)
    {
        return;
    }

    // What it wrote last may still be unread.

    slotReadStdout();
    slotReadStderr();

    QProcess* const proc = m_proc;
    m_proc               = nullptr;
    proc->disconnect(this);
    proc->deleteLater();

    if (m_shuttingDown)
    {
        failPending(QString());
        return;
    }

    const bool clean = ((status == QProcess::NormalExit) && (exitCode == 0));

    if (clean && m_pending.isEmpty())
    {
        // Nothing lost: the next request starts a new one.

        return;
    }

    const QString message = explainFailure(QString::fromUtf8(m_stderr),
                                           (status == QProcess::NormalExit) ? exitCode : -1,
                                           false);
    qCWarning(DKICLOUD_LOG) << "The iCloud Photos engine stopped:" << exitCode << m_stderr.right(500);

    failPending(message);
    Q_EMIT engineFailed(message);
}

QString ICloudEngine::explainFailure(const QString& stderrTail, int exitCode, bool failedToStart)
{
    if (failedToStart)
    {
        return QStringLiteral("Couldn't start the iCloud Photos engine: “%1” was not found.\n\n"
                              "Install Python 3 (on Arch Linux: sudo pacman -S python), "
                              "or set DKICLOUD_PYTHON to the Python interpreter to use.")
               .arg(python());
    }

    if (stderrTail.contains(QLatin1String("No module named 'requests'")))
    {
        return QStringLiteral("The iCloud Photos engine needs the Python module “requests”.\n\n"
                              "Install it (on Arch Linux: sudo pacman -S python-requests) and try again.");
    }

    if (stderrTail.contains(QLatin1String("No module named dkicloudphotos")) ||
        stderrTail.contains(QLatin1String("No module named 'dkicloudphotos'")))
    {
        return QStringLiteral("The iCloud Photos engine is missing from %1.\n\n"
                              "Reinstall digikam-icloud-photos.").arg(engineDir());
    }

    QStringList lines = stderrTail.trimmed().split(QLatin1Char('\n'), Qt::SkipEmptyParts);

    if (lines.size() > 12)
    {
        lines = lines.mid(lines.size() - 12);
    }

    QString message = (exitCode >= 0)
                    ? QStringLiteral("The iCloud Photos engine stopped unexpectedly (exit code %1).").arg(exitCode)
                    : QStringLiteral("The iCloud Photos engine crashed.");

    if (!lines.isEmpty())
    {
        message += QStringLiteral("\n\n") + lines.join(QLatin1Char('\n'));
    }

    return message;
}

} // namespace DigikamGenericICloudPhotosPlugin
