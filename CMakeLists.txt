#
# digikam-icloud-photos: an iCloud Photos import/export plugin for digiKam.
#
# SPDX-License-Identifier: GPL-3.0-or-later
#

cmake_minimum_required(VERSION 3.20)

project(digikam-icloud-photos VERSION 1.0.0 LANGUAGES CXX)

include(GNUInstallDirs)

option(BUILD_TESTING "Build the tests" OFF)

set(CMAKE_CXX_STANDARD          17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC               ON)
set(CMAKE_AUTORCC               ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Widgets)
find_package(DigikamCore   REQUIRED)
find_package(DigikamPlugin REQUIRED)

# digiKam only looks for generic plugins in its own plugin folder.
set(PLUGIN_INSTALL_DIR "${DPluginGenericInstallPath}" CACHE PATH
    "Where digiKam loads generic plugins from")

# The Python engine the plugin starts (python3 -m dkicloudphotos serve).
set(ENGINE_INSTALL_DIR "${CMAKE_INSTALL_FULL_DATADIR}/digikam-icloud-photos")

add_subdirectory(plugin)

install(DIRECTORY   engine/dkicloudphotos
        DESTINATION ${ENGINE_INSTALL_DIR}
        FILES_MATCHING
        PATTERN "*.py"
        PATTERN "__pycache__" EXCLUDE)

configure_file(packaging/digikam-icloud-photos.sh.in
               ${CMAKE_CURRENT_BINARY_DIR}/digikam-icloud-photos @ONLY)

install(PROGRAMS    ${CMAKE_CURRENT_BINARY_DIR}/digikam-icloud-photos
        DESTINATION ${CMAKE_INSTALL_BINDIR})

if(BUILD_TESTING)
    enable_testing()
    add_subdirectory(tests)
endif()
