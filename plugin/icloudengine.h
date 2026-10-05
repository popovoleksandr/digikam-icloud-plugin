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

#pragma once

// C++ includes

#include <functional>

// Qt includes

#include <QByteArray>
#include <QHash>
#include <QJsonObject>
#include <QObject>
#include <QPointer>
#include <QProcess>
#include <QString>

namespace DigikamGenericICloudPhotosPlugin
{

/**
 * One engine process per window. Requests are queued by the engine and
 * answered in order; each gets exactly one reply, and long ones send
 * events before it.
 */
class ICloudEngine : public QObject
{
    Q_OBJECT

public:

    /// Called for each event of a request.
    using EventHandler = std::function<void(const QJsonObject& event)>;

    /// Called once with the reply: the result if ok, else the error object
    /// ({"kind": ..., "message": ...}).
    using ReplyHandler = std::function<void(bool ok, const QJsonObject& resultOrError)>;

public:

    explicit ICloudEngine(QObject* const parent = nullptr);
    ~ICloudEngine() override;

    /// Starts the process if it is not running. False (and engineFailed) if it can't.
    bool start();
    bool isRunning() const;

    /// Sends a request and returns its id. If the engine can't be started the
    /// request fails with error kind "engine" (and engineFailed is emitted).
    int request(const QString& cmd, const QJsonObject& args = QJsonObject());

    /**
     * Like request(), with handlers called only while @p context still exists.
     * The handlers run on the GUI thread.
     */
    int call(const QString& cmd,
             const QJsonObject& args,
             QObject* const context,
             const ReplyHandler& onReply,
             const EventHandler& onEvent = EventHandler());

    /// Asks the running request to stop and drops the queued ones.
    void cancel();

    /// Asks the engine to exit, then makes sure it does.
    void shutdown();

    /// The interpreter (DKICLOUD_PYTHON, else python3).
    static QString python();

    /// The folder holding the dkicloudphotos package (DKICLOUD_ENGINE_DIR, else ENGINE_DIR).
    static QString engineDir();

    /// A user-facing explanation of why the engine could not run.
    static QString explainFailure(const QString& stderrTail, int exitCode, bool failedToStart);

Q_SIGNALS:

    void eventReceived(int id, const QJsonObject& event);
    void replied(int id, bool ok, const QJsonObject& resultOrError);
    void engineFailed(const QString& message);

private Q_SLOTS:

    void slotReadStdout();
    void slotReadStderr();
    void slotErrorOccurred(QProcess::ProcessError error);
    void slotFinished(int exitCode, QProcess::ExitStatus status);

private:

    void handleLine(const QByteArray& line);
    void failPending(const QString& message);
    void deliverReply(int id, bool ok, const QJsonObject& data);

private:

    struct Handlers
    {
        QPointer<QObject> context;
        bool              hasContext = false;
        ReplyHandler      onReply;
        EventHandler      onEvent;
    };

    QProcess*            m_proc         = nullptr;
    QByteArray           m_stdout;
    QByteArray           m_stderr;
    QHash<int, Handlers> m_pending;
    int                  m_nextId       = 1;
    bool                 m_shuttingDown = false;
};

} // namespace DigikamGenericICloudPhotosPlugin
