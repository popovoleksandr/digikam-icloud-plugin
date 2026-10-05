/* ============================================================
 *
 * This file is a part of digikam-icloud-photos
 *
 * Description : helpers shared by the plugin's tests.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ============================================================ */

#pragma once

// Qt includes

#include <QApplication>
#include <QByteArray>
#include <QDir>
#include <QLabel>
#include <QList>
#include <QMap>
#include <QMessageBox>
#include <QStringList>
#include <QTemporaryDir>
#include <QTest>
#include <QUrl>

// digiKam includes

#include "dinfointerface.h"

namespace TestUtils
{

/**
 * Runs @p TestClass with throw-away XDG folders and the offscreen platform.
 * Must be done before QApplication exists, hence a macro for main().
 */
#define DKICLOUD_TEST_MAIN(TestClass)                                              \
int main(int argc, char** argv)                                                    \
{                                                                                  \
    QTemporaryDir xdg;                                                             \
    qputenv("QT_QPA_PLATFORM", "offscreen");                                       \
    qputenv("XDG_CONFIG_HOME", QFile::encodeName(xdg.path() + QStringLiteral("/config"))); \
    qputenv("XDG_DATA_HOME",   QFile::encodeName(xdg.path() + QStringLiteral("/data")));   \
    qputenv("XDG_CACHE_HOME",  QFile::encodeName(xdg.path() + QStringLiteral("/cache")));  \
    qputenv("DKICLOUD_HOME",   QFile::encodeName(xdg.path() + QStringLiteral("/engine"))); \
    QApplication app(argc, argv);                                                  \
    TestClass test;                                                                \
    return QTest::qExec(&test, argc, argv);                                        \
}

/// digiKam's side of the plugin, recording what the plugin asks of it.
class FakeIface : public Digikam::DInfoInterface
{
public:

    explicit FakeIface(QObject* const parent = nullptr)
        : DInfoInterface(parent)
    {
    }

    QUrl uploadUrl() const override
    {
        return QUrl::fromLocalFile(dest);
    }

    QWidget* uploadWidget(QWidget* const parent) const override
    {
        return new QLabel(dest, parent);
    }

    QList<QUrl> currentSelectedItems() const override
    {
        return selection;
    }

    void slotMetadataChangedForUrl(const QUrl& url) override
    {
        scanned << url;
    }

    void setItemInfo(const QUrl& url, const DInfoMap& map) override
    {
        infos[url] = map;
    }

public:

    QString                  dest;
    QList<QUrl>              selection;
    QList<QUrl>              scanned;
    QMap<QUrl, DInfoMap>     infos;
};

/// How many message boxes are open.
inline int messageBoxCount()
{
    int count          = 0;
    const auto widgets = QApplication::topLevelWidgets();

    for (QWidget* const w : widgets)
    {
        if (qobject_cast<QMessageBox*>(w) && w->isVisible())
        {
            ++count;
        }
    }

    return count;
}

/// Closes the open message boxes and returns their texts.
inline QStringList closeMessageBoxes()
{
    QStringList texts;
    const auto widgets = QApplication::topLevelWidgets();

    for (QWidget* const w : widgets)
    {
        QMessageBox* const box = qobject_cast<QMessageBox*>(w);

        if (box && box->isVisible())
        {
            texts << box->text();
            box->close();
        }
    }

    return texts;
}

/// Every file below @p dir, relative to it.
inline QStringList filesBelow(const QString& dir)
{
    QStringList files;
    QDir root(dir);
    const QFileInfoList infos = root.entryInfoList(QDir::Files | QDir::Dirs | QDir::NoDotAndDotDot);

    for (const QFileInfo& info : infos)
    {
        if (info.isDir())
        {
            for (const QString& sub : filesBelow(info.absoluteFilePath()))
            {
                files << info.fileName() + QLatin1Char('/') + sub;
            }
        }
        else
        {
            files << info.fileName();
        }
    }

    files.sort();

    return files;
}

} // namespace TestUtils
