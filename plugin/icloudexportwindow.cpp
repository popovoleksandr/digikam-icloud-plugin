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

#include "icloudexportwindow.h"

// Qt includes

#include <QFileInfo>
#include <QGroupBox>
#include <QJsonArray>
#include <QLabel>
#include <QList>
#include <QLocale>
#include <QMessageBox>
#include <QPushButton>
#include <QUrl>

// digiKam includes

#include "ditemslist.h"
#include "dprogresswdg.h"

// Local includes

#include "icloudengine.h"

using namespace Digikam;

namespace DigikamGenericICloudPhotosPlugin
{

namespace
{

QString plural(qint64 n, const QString& one, const QString& many)
{
    return QStringLiteral("%1 %2").arg(QLocale().toString(n), (n == 1) ? one : many);
}

} // namespace

class Q_DECL_HIDDEN ICloudExportWindow::Private
{
public:

    Private() = default;

    ICloudSettingsWidget* widget    = nullptr;
    QLabel*               statusLbl = nullptr;
    bool                  signedIn  = false;

    // The running upload, in the order sent.

    QList<QUrl>           urls;
    int                   uploaded  = 0;
    int                   skipped   = 0;
    QStringList           failures;
};

ICloudExportWindow::ICloudExportWindow(DInfoInterface* const iface, QWidget* const parent)
    : ICloudToolWindow(iface, parent, QStringLiteral("iCloud Photos Export Dialog")),
      d(new Private)
{
    d->widget = new ICloudSettingsWidget(this, this->iface(), QStringLiteral("iCloud Photos"),
                                         QStringLiteral("Upload photos and videos to your iCloud Photos library."));

    // iCloud gets the original files, into the library rather than an album:
    // none of the resize, album or destination settings apply.

    d->widget->getAlbumBox()->hide();
    d->widget->getSizeBox()->hide();
    d->widget->getOptionsBox()->hide();
    d->widget->getUploadBox()->hide();

    QLabel* const info = new QLabel(QStringLiteral(
        "Original files are uploaded to your iCloud Photos library (Recents), not into an album.\n\n"
        "Photos that came from iCloud, or that were uploaded before, are skipped."),
        d->widget->getSettingsBox());
    info->setWordWrap(true);

    d->statusLbl = new QLabel(d->widget->getSettingsBox());
    d->statusLbl->setWordWrap(true);
    d->statusLbl->setTextInteractionFlags(Qt::TextSelectableByMouse);

    d->widget->addWidgetToSettingsBox(info);
    d->widget->addWidgetToSettingsBox(d->statusLbl);

    d->widget->imagesList()->setAllowRAW(true);

    setWindowTitle(QStringLiteral("Export to iCloud Photos"));
    startButton()->setText(QStringLiteral("Upload"));
    startButton()->setToolTip(QStringLiteral("Upload the listed items to iCloud Photos"));

    connect(startButton(), &QPushButton::clicked,
            this, &ICloudExportWindow::slotStartUpload);

    connect(d->widget->imagesList(), &DItemsList::signalImageListChanged,
            this, &ICloudExportWindow::slotImageListChanged);

    connect(d->widget->progressBar(), &DProgressWdg::signalProgressCanceled,
            engine(), &ICloudEngine::cancel);

    setSettingsWidget(d->widget);
    setStatus(QString());
    updateControls();
}

ICloudExportWindow::~ICloudExportWindow()
{
    delete d;
}

void ICloudExportWindow::accountChanged(bool signedIn)
{
    d->signedIn = signedIn;
    updateControls();
}

void ICloudExportWindow::slotImageListChanged()
{
    updateControls();
}

void ICloudExportWindow::updateControls()
{
    const bool busy = isBusy();

    startButton()->setEnabled(d->signedIn && !busy && !d->widget->imagesList()->imageUrls().isEmpty());
    d->widget->imagesList()->enableControlButtons(!busy);
    d->widget->imagesList()->enableDragAndDrop(!busy);
}

void ICloudExportWindow::setStatus(const QString& text)
{
    d->statusLbl->setText(text);
    d->statusLbl->setVisible(!text.isEmpty());
}

void ICloudExportWindow::slotStartUpload()
{
    if (isBusy())
    {
        return;
    }

    DItemsList* const list = d->widget->imagesList();
    list->clearProcessedStatus();

    d->urls.clear();
    QJsonArray files;

    const QList<QUrl> urls = list->imageUrls();

    for (const QUrl& url : urls)
    {
        if (url.isLocalFile())
        {
            d->urls << url;
            files.append(QFileInfo(url.toLocalFile()).absoluteFilePath());
        }
    }

    if (files.isEmpty())
    {
        showMessage(QMessageBox::Warning, windowTitle(), QStringLiteral("Add the photos to upload first."));
        return;
    }

    d->uploaded = 0;
    d->skipped  = 0;
    d->failures.clear();

    setBusy(true);
    updateControls();

    DProgressWdg* const bar = d->widget->progressBar();
    bar->setMaximum(int(files.size()));
    bar->setValue(0);
    bar->show();
    bar->progressScheduled(QStringLiteral("Upload to iCloud Photos"), true, true);
    bar->progressThumbnailChanged(windowIcon().pixmap(22, 22));

    list->processing(d->urls.first());
    setStatus(QStringLiteral("Uploading %1…").arg(plural(files.size(), QStringLiteral("file"), QStringLiteral("files"))));

    engine()->call(QStringLiteral("upload"), QJsonObject{ { QStringLiteral("files"), files } }, this,
                   [this](bool ok, const QJsonObject& reply) { uploadFinished(ok, reply);  },
                   [this](const QJsonObject& event)          { handleUploadEvent(event); });
}

void ICloudExportWindow::cancelRequested()
{
    if (isBusy())
    {
        setStatus(QStringLiteral("Stopping…"));
    }
}

void ICloudExportWindow::handleUploadEvent(const QJsonObject& event)
{
    const QString type = event.value(QStringLiteral("event")).toString();

    if (type == QLatin1String("message"))
    {
        setStatus(event.value(QStringLiteral("text")).toString());
        return;
    }

    if (type != QLatin1String("file"))
    {
        return;
    }

    DItemsList* const list = d->widget->imagesList();
    const QString path     = event.value(QStringLiteral("path")).toString();
    const QString status   = event.value(QStringLiteral("status")).toString();
    const QString message  = event.value(QStringLiteral("message")).toString();
    const QUrl url         = QUrl::fromLocalFile(path);
    const int done         = event.value(QStringLiteral("done")).toInt();
    const int total        = event.value(QStringLiteral("total")).toInt(int(d->urls.size()));

    if (status == QLatin1String("uploaded"))
    {
        d->uploaded++;
        list->processed(url, true);
    }
    else if (status == QLatin1String("skipped"))
    {
        d->skipped++;
        list->processed(url, true);
    }
    else
    {
        d->failures << QStringLiteral("%1: %2").arg(QFileInfo(path).fileName(),
                                                    message.isEmpty() ? QStringLiteral("failed") : message);
        list->processed(url, false);
    }

    // The engine goes through the files in the order sent.

    if ((done >= 0) && (done < d->urls.size()))
    {
        list->processing(d->urls.at(done));
    }

    DProgressWdg* const bar = d->widget->progressBar();
    bar->setValue(qMin(done, bar->maximum()));

    const QString text = QStringLiteral("Uploaded %1 of %2.").arg(QLocale().toString(done), QLocale().toString(total));
    setStatus(text);
    bar->progressStatusChanged(text);
}

void ICloudExportWindow::uploadFinished(bool ok, const QJsonObject& reply)
{
    setBusy(false);
    updateControls();

    DProgressWdg* const bar = d->widget->progressBar();
    bar->progressCompleted();
    bar->hide();

    if (!ok)
    {
        // Items still marked as being uploaded.

        d->widget->imagesList()->cancelProcess();

        const QString kind = reply.value(QStringLiteral("kind")).toString();

        if (kind == QLatin1String("cancelled"))
        {
            setStatus(QStringLiteral("Upload stopped after %1.")
                      .arg(plural(d->uploaded, QStringLiteral("file"), QStringLiteral("files"))));
        }
        else
        {
            setStatus(QStringLiteral("The upload failed."));
            reportError(reply, QStringLiteral("The upload to iCloud Photos failed"));
        }

        Q_EMIT signalUploadFinished(false, reply);
        return;
    }

    const qint64 uploaded = reply.value(QStringLiteral("uploaded")).toInt(d->uploaded);
    const qint64 skipped  = reply.value(QStringLiteral("skipped")).toInt(d->skipped);
    const qint64 failed   = reply.value(QStringLiteral("failed")).toInt(d->failures.size());

    QStringList lines;
    lines << QStringLiteral("Uploaded %1.").arg(plural(uploaded, QStringLiteral("file"), QStringLiteral("files")));

    if (skipped > 0)
    {
        lines << QStringLiteral("Skipped %1 already in iCloud Photos.")
                 .arg(plural(skipped, QStringLiteral("file"), QStringLiteral("files")));
    }

    if (failed > 0)
    {
        lines << QStringLiteral("%1 could not be uploaded.")
                 .arg(plural(failed, QStringLiteral("file"), QStringLiteral("files")));
    }

    setStatus(lines.join(QLatin1Char(' ')));
    showMessage((failed > 0) ? QMessageBox::Warning : QMessageBox::Information,
                windowTitle(), lines.join(QStringLiteral("\n\n")), d->failures.join(QLatin1Char('\n')));

    Q_EMIT signalUploadFinished(true, reply);
}

} // namespace DigikamGenericICloudPhotosPlugin
