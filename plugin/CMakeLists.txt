#
# SPDX-License-Identifier: GPL-3.0-or-later
#

# Everything but the plugin entry point, so the tests can use the windows too.
add_library(icloudphotos_objects OBJECT
    icloudengine.cpp
    icloudsession.cpp
    icloudtoolwindow.cpp
    icloudlogindialog.cpp
    icloudimportwindow.cpp
    icloudexportwindow.cpp
    icloudphotos.qrc
)

set_target_properties(icloudphotos_objects PROPERTIES POSITION_INDEPENDENT_CODE ON)

target_compile_definitions(icloudphotos_objects PUBLIC
    ENGINE_DIR="${ENGINE_INSTALL_DIR}"
    PLUGIN_VERSION="${PROJECT_VERSION}"
)

target_include_directories(icloudphotos_objects PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

target_link_libraries(icloudphotos_objects PUBLIC
    Digikam::digikamcore
    Qt6::Core
    Qt6::Gui
    Qt6::Widgets
)

target_compile_options(icloudphotos_objects PRIVATE -Wall -Wextra)

add_library(Generic_ICloudPhotos_Plugin MODULE icloudphotosplugin.cpp)

# digiKam only loads files called Generic_*.so.
set_target_properties(Generic_ICloudPhotos_Plugin PROPERTIES PREFIX "")

# Linking an object library links its object files in.
target_link_libraries(Generic_ICloudPhotos_Plugin PRIVATE icloudphotos_objects)

target_compile_options(Generic_ICloudPhotos_Plugin PRIVATE -Wall -Wextra)

install(TARGETS Generic_ICloudPhotos_Plugin DESTINATION ${PLUGIN_INSTALL_DIR})
