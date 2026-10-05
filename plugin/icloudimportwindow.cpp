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

#include "icloudimportwindow.h"

// Qt includes

#include <QCheckBox>
#include <QComboBox>
#include <QFile>
#include <QFileInfo>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHash>
#include <QHeaderView>
#include <QIcon>
#include <QJsonArray>
#include <QLabel>
#include <QLocale>
#include <QMessageBox>
#include <QPushButton>
#include <QSettings>
#include <QTreeWidget>
#include <QUrl>
#include <QVBoxLayout>

// digiKam includes

#include "dmetadata.h"
#include "dprogresswdg.h"

// Local includes

#include "icloudengine.h"
#include "icloudphotos_debug.h"

using namespace Digikam;

namespace DigikamGenericICloudPhotosPlugin
{

namespace
{

const QString SETTINGS_GROUP = QStringLiteral("Import");
const QString DEFAULT_LIB    = QStringLiteral("private/PrimarySync");
const QString ALL_PHOTOS     = QStringLiteral("all");
const QString TAG_ROOT       = QStringLiteral("iCloud Photos");

enum AlbumColumn
{
    NameColumn = 0,
    CountColumn
};

/// QSettings keys can't hold "/" (it means a subgroup).
QString libraryKey(const QString& library)
{
    QString key = library;

    return QStringLiteral("Albums_") + key.replace(QLatin1Char('/'), QLatin1Char('_'));
}

QStringList toStringList(const QJsonValue& value)
{
    QStringList list;

    for (const QJsonValue& v : value.toArray())
    {
        if (!v.toString().isEmpty())
        {
            list << v.toString();
        }
    }

    return list;
}

QString plural(qint64 n, const QString& one, const QString& many)
{
    return QStringLiteral("%1 %2").arg(QLocale().toString(n), (n == 1) ? one : many);
}

} // namespace

class Q_DECL_HIDDEN ICloudImportWindow::Private
{
public:

    Private() = default;

    ICloudSettingsWidget*       widget        = nullptr;

    QComboBox*                  libraryCoB    = nullptr;
    QLabel*                     libraryLbl    = nullptr;
    QPushButton*                reloadBtn     = nullptr;
    QTreeWidget*                albumTree     = nullptr;

    QGroupBox*                  optionsBox    = nullptr;
    QComboBox*                  layoutCoB     = nullptr;
    QCheckBox*                  videosChB     = nullptr;
    QCheckBox*                  liveChB       = nullptr;
    QCheckBox*                  editedChB     = nullptr;
    QCheckBox*                  tagsChB       = nullptr;
    QCheckBox*                  redownloadChB = nullptr;
    QLabel*                     statusLbl     = nullptr;

    bool                        signedIn      = false;
    bool                        loading       = false;
    QString                     savedLibrary;
    QString                     treeLibrary;                   ///< whose albums the tree shows
    QHash<QString, QStringList> savedAlbums;                   ///< library -> ticked album ids

    // The running sync.

    bool                        metadata      = false;
    int                         total         = 0;
    int                         done          = 0;
    int                         downloaded    = 0;
    int                         adopted       = 0;
    QStringList                 errors;
};

ICloudImportWindow::ICloudImportWindow(DInfoInterface* const iface, QWidget* const parent)
    : ICloudToolWindow(iface, parent, QStringLiteral("iCloud Photos Import Dialog")),
      d(new Private)
{
    d->widget = new ICloudSettingsWidget(this, this->iface(), QStringLiteral("iCloud Photos"),
                                         QStringLiteral("Download the photos of your iCloud albums "
                                                        "that are not in digiKam yet."));

    // -- Left: the iCloud albums, in place of the image list -----------------

    QWidget* const albumPanel = new QWidget(d->widget);
    QVBoxLayout* const panLay = new QVBoxLayout(albumPanel);
    panLay->setContentsMargins(QMargins());

    QWidget* const libRow    = new QWidget(albumPanel);
    QHBoxLayout* const libLay = new QHBoxLayout(libRow);
    libLay->setContentsMargins(QMargins());
    d->libraryLbl = new QLabel(QStringLiteral("Library:"), libRow);
    d->libraryCoB = new QComboBox(libRow);
    d->reloadBtn  = new QPushButton(QIcon::fromTheme(QStringLiteral("view-refresh")),
                                    QStringLiteral("Reload"), libRow);
    d->reloadBtn->setToolTip(QStringLiteral("List your iCloud albums again"));
    libLay->addWidget(d->libraryLbl);
    libLay->addWidget(d->libraryCoB, 1);
    libLay->addStretch(0);
    libLay->addWidget(d->reloadBtn);
    d->libraryLbl->hide();
    d->libraryCoB->hide();

    d->albumTree = new QTreeWidget(albumPanel);
    d->albumTree->setColumnCount(2);
    d->albumTree->setHeaderLabels({ QStringLiteral("iCloud Album"), QStringLiteral("Items") });
    d->albumTree->setRootIsDecorated(false);
    d->albumTree->setUniformRowHeights(true);
    d->albumTree->setAlternatingRowColors(true);
    d->albumTree->header()->setSectionResizeMode(NameColumn,  QHeaderView::Stretch);
    d->albumTree->header()->setSectionResizeMode(CountColumn, QHeaderView::ResizeToContents);
    d->albumTree->header()->setStretchLastSection(false);
    d->albumTree->setWhatsThis(QStringLiteral("Tick the iCloud albums to download. Only photos that "
                                              "are not on this computer yet are downloaded."));

    QLabel* const hint = new QLabel(QStringLiteral("Tick the albums to download. Photos already "
                                                   "downloaded are skipped."), albumPanel);
    hint->setWordWrap(true);

    panLay->addWidget(libRow);
    panLay->addWidget(d->albumTree, 1);
    panLay->addWidget(hint);

    d->widget->replaceImageList(albumPanel);

    // -- Right: destination and options ---------------------------------------

    d->widget->getAlbumBox()->hide();
    d->widget->getSizeBox()->hide();
    d->widget->getOptionsBox()->hide();
    d->widget->getUploadBox()->setTitle(QStringLiteral("Download to"));
    d->widget->getUploadBox()->setWhatsThis(QStringLiteral("The digiKam album the photos are downloaded into."));

    d->optionsBox              = new QGroupBox(QStringLiteral("Options"), d->widget->getSettingsBox());
    QGridLayout* const optLay  = new QGridLayout(d->optionsBox);

    d->layoutCoB = new QComboBox(d->optionsBox);
    d->layoutCoB->addItem(QStringLiteral("Year/Month subfolders"), QStringLiteral("date"));
    d->layoutCoB->addItem(QStringLiteral("All in one folder"),     QStringLiteral("flat"));

    d->videosChB     = new QCheckBox(QStringLiteral("Include videos"), d->optionsBox);
    d->liveChB       = new QCheckBox(QStringLiteral("Include Live Photo videos"), d->optionsBox);
    d->editedChB     = new QCheckBox(QStringLiteral("Also download edited versions"), d->optionsBox);
    d->tagsChB       = new QCheckBox(QStringLiteral("Add iCloud albums as tags and favorites as 5 stars"), d->optionsBox);
    d->redownloadChB = new QCheckBox(QStringLiteral("Download again photos I deleted here"), d->optionsBox);

    d->liveChB->setToolTip(QStringLiteral("Saves the short video of a Live Photo next to the photo (IMG_1234.MOV)."));
    d->editedChB->setToolTip(QStringLiteral("Saves photos edited on your iPhone, iPad or Mac a second time, "
                                            "as name-edited.jpg, next to the original."));
    d->tagsChB->setToolTip(QStringLiteral("Tags each photo “iCloud Photos/<album>” and rates favorites "
                                          "5 stars. Lists every album, so it takes a little longer."));
    d->redownloadChB->setToolTip(QStringLiteral("By default, photos you deleted or moved away after a sync "
                                                "are not downloaded again."));

    optLay->addWidget(new QLabel(QStringLiteral("Folders:"), d->optionsBox), 0, 0);
    optLay->addWidget(d->layoutCoB,     0, 1);
    optLay->addWidget(d->videosChB,     1, 0, 1, 2);
    optLay->addWidget(d->liveChB,       2, 0, 1, 2);
    optLay->addWidget(d->editedChB,     3, 0, 1, 2);
    optLay->addWidget(d->tagsChB,       4, 0, 1, 2);
    optLay->addWidget(d->redownloadChB, 5, 0, 1, 2);
    optLay->setColumnStretch(1, 1);

    d->statusLbl = new QLabel(d->widget->getSettingsBox());
    d->statusLbl->setWordWrap(true);
    d->statusLbl->setTextInteractionFlags(Qt::TextSelectableByMouse);

    d->widget->addWidgetToSettingsBox(d->optionsBox);
    d->widget->addWidgetToSettingsBox(d->statusLbl);

    // -- Dialog ---------------------------------------------------------------

    setWindowTitle(QStringLiteral("Import from iCloud Photos"));
    startButton()->setText(QStringLiteral("Sync"));
    startButton()->setToolTip(QStringLiteral("Download the photos of the ticked albums that are not here yet"));

    connect(startButton(), &QPushButton::clicked,
            this, &ICloudImportWindow::slotStartSync);

    connect(d->reloadBtn, &QPushButton::clicked,
            this, &ICloudImportWindow::slotReload);

    connect(d->libraryCoB, qOverload<int>(&QComboBox::activated),
            this, &ICloudImportWindow::slotLibraryActivated);

    connect(d->albumTree, &QTreeWidget::itemChanged,
            this, &ICloudImportWindow::slotAlbumItemChanged);

    connect(d->widget->progressBar(), &DProgressWdg::signalProgressCanceled,
            this, &ICloudImportWindow::cancelRequested);

    connect(d->widget->progressBar(), &DProgressWdg::signalProgressCanceled,
            engine(), &ICloudEngine::cancel);

    setSettingsWidget(d->widget);
    setStatus(QString());
    updateControls();
}

ICloudImportWindow::~ICloudImportWindow()
{
    delete d;
}

// -- Settings -------------------------------------------------------------------

void ICloudImportWindow::readSettings()
{
    QSettings settings(QStringLiteral("digikam-icloud-photos"), QStringLiteral("plugin"));
    settings.beginGroup(SETTINGS_GROUP);

    const int layout = d->layoutCoB->findData(settings.value(QStringLiteral("Layout"), QStringLiteral("date")));
    d->layoutCoB->setCurrentIndex(qMax(0, layout));
    d->videosChB->setChecked(settings.value(QStringLiteral("IncludeVideos"), true).toBool());
    d->liveChB->setChecked(settings.value(QStringLiteral("LivePhotos"), true).toBool());
    d->editedChB->setChecked(settings.value(QStringLiteral("Edited"), false).toBool());
    d->tagsChB->setChecked(settings.value(QStringLiteral("AlbumTags"), true).toBool());
    d->redownloadChB->setChecked(settings.value(QStringLiteral("RedownloadDeleted"), false).toBool());
    d->savedLibrary = settings.value(QStringLiteral("Library"), DEFAULT_LIB).toString();

    const QStringList keys = settings.childKeys();

    for (const QString& key : keys)
    {
        if (key.startsWith(QLatin1String("Albums_")))
        {
            d->savedAlbums.insert(key, settings.value(key).toStringList());
        }
    }

    settings.endGroup();
}

void ICloudImportWindow::writeSettings()
{
    if (!d->treeLibrary.isEmpty() && (d->albumTree->topLevelItemCount() > 0))
    {
        d->savedAlbums.insert(libraryKey(d->treeLibrary), checkedAlbums());
    }

    QSettings settings(QStringLiteral("digikam-icloud-photos"), QStringLiteral("plugin"));
    settings.beginGroup(SETTINGS_GROUP);
    settings.setValue(QStringLiteral("Layout"),            d->layoutCoB->currentData());
    settings.setValue(QStringLiteral("IncludeVideos"),     d->videosChB->isChecked());
    settings.setValue(QStringLiteral("LivePhotos"),        d->liveChB->isChecked());
    settings.setValue(QStringLiteral("Edited"),            d->editedChB->isChecked());
    settings.setValue(QStringLiteral("AlbumTags"),         d->tagsChB->isChecked());
    settings.setValue(QStringLiteral("RedownloadDeleted"), d->redownloadChB->isChecked());

    if (!currentLibrary().isEmpty())
    {
        settings.setValue(QStringLiteral("Library"), currentLibrary());
    }

    for (auto it = d->savedAlbums.constBegin() ; it != d->savedAlbums.constEnd() ; ++it)
    {
        settings.setValue(it.key(), it.value());
    }

    settings.endGroup();
}

// -- Account, libraries and albums ------------------------------------------------

void ICloudImportWindow::accountChanged(bool signedIn)
{
    const bool wasSignedIn = d->signedIn;
    d->signedIn            = signedIn;

    if (signedIn && (!wasSignedIn || (d->albumTree->topLevelItemCount() == 0)))
    {
        loadLibraries();
    }
    else if (!signedIn)
    {
        d->albumTree->clear();
        d->treeLibrary.clear();
        setStatus(QStringLiteral("Sign in to see your iCloud albums."));
    }

    updateControls();
}

QString ICloudImportWindow::currentLibrary() const
{
    const QString lib = d->libraryCoB->currentData().toString();

    return lib.isEmpty() ? DEFAULT_LIB : lib;
}

void ICloudImportWindow::slotReload()
{
    loadLibraries();
}

void ICloudImportWindow::loadLibraries()
{
    if (d->loading)
    {
        return;
    }

    d->loading = true;
    updateControls();
    setStatus(QStringLiteral("Loading your iCloud Photos libraries…"));

    engine()->call(QStringLiteral("libraries"), QJsonObject(), this,
        [this](bool ok, const QJsonObject& reply)
        {
            d->loading = false;

            if (!ok)
            {
                setStatus(QString());
                updateControls();
                reportError(reply, QStringLiteral("Couldn't list your iCloud Photos libraries"));
                return;
            }

            const QString previous = d->libraryCoB->currentData().toString();
            const QString wanted   = previous.isEmpty() ? d->savedLibrary : previous;
            const QJsonArray libs  = reply.value(QStringLiteral("libraries")).toArray();

            d->libraryCoB->clear();

            for (const QJsonValue& value : libs)
            {
                const QJsonObject lib = value.toObject();
                d->libraryCoB->addItem(lib.value(QStringLiteral("name")).toString(),
                                       lib.value(QStringLiteral("id")).toString());
            }

            if (d->libraryCoB->count() == 0)
            {
                d->libraryCoB->addItem(QStringLiteral("Personal Library"), DEFAULT_LIB);
            }

            d->libraryCoB->setCurrentIndex(qMax(0, d->libraryCoB->findData(wanted)));

            const bool several = (d->libraryCoB->count() > 1);
            d->libraryLbl->setVisible(several);
            d->libraryCoB->setVisible(several);

            loadAlbums();
        },
        [this](const QJsonObject& event)
        {
            if (event.value(QStringLiteral("event")).toString() == QLatin1String("message"))
            {
                setStatus(event.value(QStringLiteral("text")).toString());
            }
        }
    );
}

void ICloudImportWindow::slotLibraryActivated(int)
{
    if (currentLibrary() != d->treeLibrary)
    {
        loadAlbums();
    }
}

void ICloudImportWindow::loadAlbums()
{
    if (!d->treeLibrary.isEmpty() && (d->albumTree->topLevelItemCount() > 0))
    {
        d->savedAlbums.insert(libraryKey(d->treeLibrary), checkedAlbums());
    }

    const QString library = currentLibrary();

    d->albumTree->clear();
    d->treeLibrary.clear();
    d->loading = true;
    updateControls();
    setStatus(QStringLiteral("Loading your iCloud albums…"));

    engine()->call(QStringLiteral("albums"), QJsonObject{ { QStringLiteral("library"), library } }, this,
        [this, library](bool ok, const QJsonObject& reply)
        {
            d->loading = false;

            if (!ok)
            {
                setStatus(QString());
                updateControls();
                reportError(reply, QStringLiteral("Couldn't list your iCloud albums"));
                return;
            }

            const QStringList saved = d->savedAlbums.value(libraryKey(library), { ALL_PHOTOS });
            const QJsonArray albums = reply.value(QStringLiteral("albums")).toArray();
            bool anyChecked         = false;

            const QSignalBlocker blocker(d->albumTree);

            for (const QJsonValue& value : albums)
            {
                const QJsonObject album = value.toObject();
                const QString id        = album.value(QStringLiteral("id")).toString();
                const bool smart        = (album.value(QStringLiteral("kind")).toString() == QLatin1String("smart"));
                QString name            = album.value(QStringLiteral("name")).toString();
                const QString path      = album.value(QStringLiteral("path")).toString();

                if (!smart && !path.isEmpty())
                {
                    name = path;
                }

                QTreeWidgetItem* const item = new QTreeWidgetItem(d->albumTree);
                item->setText(NameColumn, name);
                item->setData(NameColumn, Qt::UserRole, id);
                item->setFlags((item->flags() | Qt::ItemIsUserCheckable) & ~Qt::ItemIsEditable);
                item->setIcon(NameColumn, QIcon::fromTheme(smart ? QStringLiteral("folder-pictures")
                                                                 : QStringLiteral("folder-image")));

                const QJsonValue count = album.value(QStringLiteral("count"));

                if (count.isDouble())
                {
                    item->setText(CountColumn, QLocale().toString(qint64(count.toDouble())));
                    item->setTextAlignment(CountColumn, Qt::AlignRight | Qt::AlignVCenter);
                }

                if (id == ALL_PHOTOS)
                {
                    QFont font = item->font(NameColumn);
                    font.setBold(true);
                    item->setFont(NameColumn, font);
                }

                const bool checked = saved.contains(id);
                anyChecked        |= checked;
                item->setCheckState(NameColumn, checked ? Qt::Checked : Qt::Unchecked);
            }

            if (!anyChecked)
            {
                for (int i = 0 ; i < d->albumTree->topLevelItemCount() ; ++i)
                {
                    QTreeWidgetItem* const item = d->albumTree->topLevelItem(i);

                    if (item->data(NameColumn, Qt::UserRole).toString() == ALL_PHOTOS)
                    {
                        item->setCheckState(NameColumn, Qt::Checked);
                    }
                }
            }

            d->treeLibrary = library;
            setStatus(QString());
            updateControls();

            Q_EMIT signalAlbumsLoaded();
        },
        [this](const QJsonObject& event)
        {
            if (event.value(QStringLiteral("event")).toString() == QLatin1String("message"))
            {
                setStatus(event.value(QStringLiteral("text")).toString());
            }
        }
    );
}

QStringList ICloudImportWindow::checkedAlbums() const
{
    QStringList ids;

    for (int i = 0 ; i < d->albumTree->topLevelItemCount() ; ++i)
    {
        const QTreeWidgetItem* const item = d->albumTree->topLevelItem(i);

        if (item->checkState(NameColumn) == Qt::Checked)
        {
            ids << item->data(NameColumn, Qt::UserRole).toString();
        }
    }

    return ids;
}

QStringList ICloudImportWindow::albumNames() const
{
    QStringList names;

    for (int i = 0 ; i < d->albumTree->topLevelItemCount() ; ++i)
    {
        names << d->albumTree->topLevelItem(i)->text(NameColumn);
    }

    return names;
}

int ICloudImportWindow::albumCount() const
{
    return d->albumTree->topLevelItemCount();
}

void ICloudImportWindow::slotAlbumItemChanged()
{
    updateControls();
}

void ICloudImportWindow::updateControls()
{
    const bool busy  = isBusy();
    const bool ready = d->signedIn && !busy && !d->loading;

    startButton()->setEnabled(ready && !checkedAlbums().isEmpty());
    d->albumTree->setEnabled(d->signedIn && !busy);
    d->reloadBtn->setEnabled(ready);
    d->libraryCoB->setEnabled(ready);
    d->optionsBox->setEnabled(!busy);
    d->widget->getUploadBox()->setEnabled(!busy);
}

void ICloudImportWindow::setStatus(const QString& text)
{
    d->statusLbl->setText(text);
    d->statusLbl->setVisible(!text.isEmpty());
}

// -- Sync -------------------------------------------------------------------------

void ICloudImportWindow::slotStartSync()
{
    if (isBusy())
    {
        return;
    }

    const QString dest = d->widget->getDestinationPath();
    const QFileInfo destInfo(dest);

    if (dest.isEmpty() || !destInfo.isDir())
    {
        showMessage(QMessageBox::Warning, windowTitle(),
                    QStringLiteral("Choose the digiKam album to download into, under “Download to”."));
        return;
    }

    if (!destInfo.isWritable())
    {
        showMessage(QMessageBox::Warning, windowTitle(),
                    QStringLiteral("You can't write to %1. Choose another album.").arg(dest));
        return;
    }

    const QStringList albums = checkedAlbums();

    if (albums.isEmpty())
    {
        showMessage(QMessageBox::Warning, windowTitle(), QStringLiteral("Tick at least one iCloud album."));
        return;
    }

    writeSettings();

    const QJsonObject args
    {
        { QStringLiteral("dest"),               destInfo.absoluteFilePath()              },
        { QStringLiteral("library"),            currentLibrary()                         },
        { QStringLiteral("albums"),             QJsonArray::fromStringList(albums)       },
        { QStringLiteral("layout"),             d->layoutCoB->currentData().toString()   },
        { QStringLiteral("include_videos"),     d->videosChB->isChecked()                },
        { QStringLiteral("live_photos"),        d->liveChB->isChecked()                  },
        { QStringLiteral("edited"),             d->editedChB->isChecked()                },
        { QStringLiteral("album_tags"),         d->tagsChB->isChecked()                  },
        { QStringLiteral("redownload_deleted"), d->redownloadChB->isChecked()            },
    };

    d->metadata   = d->tagsChB->isChecked();
    d->total      = 0;
    d->done       = 0;
    d->downloaded = 0;
    d->adopted    = 0;
    d->errors.clear();

    setBusy(true);
    updateControls();

    DProgressWdg* const bar = d->widget->progressBar();
    bar->setMaximum(0);             // busy indicator until we know how many
    bar->setValue(0);
    bar->show();
    bar->progressScheduled(QStringLiteral("Sync with iCloud Photos"), true, true);
    bar->progressThumbnailChanged(windowIcon().pixmap(22, 22));

    setStatus(QStringLiteral("Starting…"));

    engine()->call(QStringLiteral("sync"), args, this,
                   [this](bool ok, const QJsonObject& reply) { syncFinished(ok, reply); },
                   [this](const QJsonObject& event)          { handleSyncEvent(event);  });
}

void ICloudImportWindow::cancelRequested()
{
    if (isBusy())
    {
        setStatus(QStringLiteral("Stopping…"));
    }
}

void ICloudImportWindow::handleSyncEvent(const QJsonObject& event)
{
    const QString type      = event.value(QStringLiteral("event")).toString();
    DProgressWdg* const bar = d->widget->progressBar();

    if ((type == QLatin1String("phase")) || (type == QLatin1String("message")))
    {
        const QString text = event.value(QStringLiteral("text")).toString();
        setStatus(text);
        bar->progressStatusChanged(text);
    }
    else if (type == QLatin1String("total"))
    {
        d->total            = event.value(QStringLiteral("total")).toInt();
        const qint64 bytes  = qint64(event.value(QStringLiteral("bytes")).toDouble());

        bar->setMaximum(qMax(d->total, 1));
        bar->setValue(0);

        setStatus(d->total ? QStringLiteral("%1 to download (%2).")
                                 .arg(plural(d->total, QStringLiteral("file"), QStringLiteral("files")), humanSize(bytes))
                           : QStringLiteral("Nothing new to download."));
    }
    else if (type == QLatin1String("file"))
    {
        registerFile(event);

        d->done = event.value(QStringLiteral("done")).toInt(d->done + 1);

        if (bar->maximum() > 0)
        {
            bar->setValue(qMin(d->done, bar->maximum()));
        }
    }
    else if (type == QLatin1String("progress"))
    {
        d->done                 = event.value(QStringLiteral("done")).toInt(d->done);
        const int total         = event.value(QStringLiteral("total")).toInt(d->total);
        const qint64 bytesDone  = qint64(event.value(QStringLiteral("bytes_done")).toDouble());
        const qint64 bytesTotal = qint64(event.value(QStringLiteral("bytes_total")).toDouble());

        if (total > 0)
        {
            d->total = total;
            bar->setMaximum(total);
            bar->setValue(qMin(d->done, total));
        }

        const QString text = (bytesTotal > 0)
                           ? QStringLiteral("Downloaded %1 of %2 files (%3 of %4).")
                                 .arg(QLocale().toString(d->done), QLocale().toString(d->total),
                                      humanSize(bytesDone), humanSize(bytesTotal))
                           : QStringLiteral("Downloaded %1 of %2 files.")
                                 .arg(QLocale().toString(d->done), QLocale().toString(d->total));
        setStatus(text);
        bar->progressStatusChanged(text);
    }
    else if (type == QLatin1String("error"))
    {
        const QString name = event.value(QStringLiteral("filename")).toString();
        const QString msg  = event.value(QStringLiteral("message")).toString();
        d->errors << (name.isEmpty() ? msg : QStringLiteral("%1: %2").arg(name, msg));
    }
}

void ICloudImportWindow::registerFile(const QJsonObject& event)
{
    const QString path = event.value(QStringLiteral("path")).toString();

    if (path.isEmpty() || !QFileInfo::exists(path))
    {
        return;
    }

    const QJsonObject asset = event.value(QStringLiteral("asset")).toObject();
    const bool favorite     = asset.value(QStringLiteral("favorite")).toBool();
    const bool live         = (asset.value(QStringLiteral("role")).toString() == QLatin1String("live"));

    if (event.value(QStringLiteral("status")).toString() == QLatin1String("adopted"))
    {
        d->adopted++;
    }
    else
    {
        d->downloaded++;
    }

    if (d->metadata && !live)
    {
        writeICloudMetadata(path, asset);
    }

    // Scans the file into digiKam's database right away (and its new folder).

    const QUrl url = QUrl::fromLocalFile(path);
    iface()->slotMetadataChangedForUrl(url);

    if (d->metadata && favorite && !live)
    {
        // In case the file format can't hold the rating.

        DInfoInterface::DInfoMap map;
        map.insert(QStringLiteral("rating"), 5);
        iface()->setItemInfo(url, map);
    }
}

void ICloudImportWindow::writeICloudMetadata(const QString& path, const QJsonObject& asset)
{
    const QStringList albums = toStringList(asset.value(QStringLiteral("albums")));
    const bool favorite      = asset.value(QStringLiteral("favorite")).toBool();
    const QString caption    = asset.value(QStringLiteral("caption")).toString().trimmed();

    if (albums.isEmpty() && !favorite && caption.isEmpty())
    {
        return;
    }

    const QDateTime mtime = QFileInfo(path).lastModified();
    DMetadata meta(path);
    bool changed          = false;

    if (!albums.isEmpty())
    {
        QStringList tags;
        meta.getItemTagsPath(tags);
        bool added = false;

        for (const QString& album : albums)
        {
            const QString tag = TAG_ROOT + QLatin1Char('/') + album;

            if (!tags.contains(tag))
            {
                tags << tag;
                added = true;
            }
        }

        if (added)
        {
            changed |= meta.setItemTagsPath(tags);
        }
    }

    if (favorite && (meta.getItemRating() < 5))
    {
        changed |= meta.setItemRating(5);
    }

    if (!caption.isEmpty())
    {
        CaptionsMap comments = meta.getItemComments();
        const QString lang   = QStringLiteral("x-default");

        if (comments.value(lang).caption.isEmpty())
        {
            CaptionValues value;
            value.caption = caption;
            value.date    = QDateTime::currentDateTime();
            comments.insert(lang, value);
            changed      |= meta.setItemComments(comments);
        }
    }

    if (!changed)
    {
        return;
    }

    if (!meta.applyChanges())
    {
        // HEIC and videos may be read-only without ExifTool: the scan still adds the file.

        qCDebug(DKICLOUD_LOG) << "Couldn't write the iCloud albums into" << path;
        return;
    }

    // The engine dated the file when the photo was taken; keep it that way.

    if (mtime.isValid() && (QFileInfo(path).lastModified() != mtime))
    {
        QFile file(path);

        if (file.open(QIODevice::ReadWrite))
        {
            file.setFileTime(mtime, QFileDevice::FileModificationTime);
        }
    }
}

void ICloudImportWindow::syncFinished(bool ok, const QJsonObject& reply)
{
    setBusy(false);
    updateControls();

    DProgressWdg* const bar = d->widget->progressBar();
    bar->progressCompleted();
    bar->hide();

    if (!ok)
    {
        const QString kind = reply.value(QStringLiteral("kind")).toString();

        if (kind == QLatin1String("cancelled"))
        {
            setStatus(QStringLiteral("Sync stopped after downloading %1.")
                      .arg(plural(d->downloaded, QStringLiteral("file"), QStringLiteral("files"))));
        }
        else
        {
            setStatus(QStringLiteral("The sync failed."));
            reportError(reply, QStringLiteral("The sync with iCloud Photos failed"));
        }

        Q_EMIT signalSyncFinished(false, reply);
        return;
    }

    const qint64 downloaded = reply.value(QStringLiteral("downloaded")).toInt(d->downloaded);
    const qint64 adopted    = reply.value(QStringLiteral("adopted")).toInt(d->adopted);
    const qint64 deleted    = reply.value(QStringLiteral("deleted_locally")).toInt();
    const qint64 failed     = reply.value(QStringLiteral("failed")).toInt(d->errors.size());
    const qint64 bytes      = qint64(reply.value(QStringLiteral("bytes")).toDouble());

    QStringList lines;

    if (downloaded > 0)
    {
        lines << QStringLiteral("Downloaded %1 (%2).")
                 .arg(plural(downloaded, QStringLiteral("new file"), QStringLiteral("new files")), humanSize(bytes));
    }
    else if (failed == 0)
    {
        lines << QStringLiteral("Everything is up to date.");
    }

    if (adopted > 0)
    {
        lines << QStringLiteral("%1 already here and now kept in sync.")
                 .arg(plural(adopted, QStringLiteral("file was"), QStringLiteral("files were")));
    }

    if (deleted > 0)
    {
        lines << QStringLiteral("%1 you deleted here not downloaded again.")
                 .arg(plural(deleted, QStringLiteral("file"), QStringLiteral("files")));
    }

    if (failed > 0)
    {
        lines << QStringLiteral("%1 could not be downloaded. Sync again to retry.")
                 .arg(plural(failed, QStringLiteral("file"), QStringLiteral("files")));
    }

    setStatus(lines.join(QLatin1Char(' ')));
    showMessage((failed > 0) ? QMessageBox::Warning : QMessageBox::Information,
                windowTitle(), lines.join(QStringLiteral("\n\n")), d->errors.join(QLatin1Char('\n')));

    Q_EMIT signalSyncFinished(true, reply);
}

} // namespace DigikamGenericICloudPhotosPlugin
