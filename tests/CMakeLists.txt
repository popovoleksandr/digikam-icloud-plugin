#
# SPDX-License-Identifier: GPL-3.0-or-later
#

find_package(Qt6 REQUIRED COMPONENTS Test)
find_package(Python3 COMPONENTS Interpreter)

# A dkicloudphotos package that is really the protocol stub.

set(STUB_DIR ${CMAKE_CURRENT_BINARY_DIR}/stub)
configure_file(stub_engine.py ${STUB_DIR}/dkicloudphotos/__main__.py COPYONLY)
file(WRITE ${STUB_DIR}/dkicloudphotos/__init__.py "")

set(TEST_DEFINITIONS
    STUB_ENGINE_DIR="${STUB_DIR}"
    REAL_ENGINE_DIR="${PROJECT_SOURCE_DIR}/engine"
    FAKE_ICLOUD="${CMAKE_CURRENT_SOURCE_DIR}/fake_icloud.py"
    PLUGIN_FILE="$<TARGET_FILE:Generic_ICloudPhotos_Plugin>"
    PLUGIN_DIR="$<TARGET_FILE_DIR:Generic_ICloudPhotos_Plugin>"
)

# Tests that only load the built plugin.

foreach(name plugin_load_test digikam_loader_test)
    add_executable(${name} ${name}.cpp)
    target_compile_definitions(${name} PRIVATE ${TEST_DEFINITIONS})
    target_link_libraries(${name} PRIVATE Qt6::Test Qt6::Widgets Digikam::digikamcore)
    add_dependencies(${name} Generic_ICloudPhotos_Plugin)
    add_test(NAME ${name} COMMAND ${name})
endforeach()

# Tests that use the plugin's classes directly.

foreach(name engine_test windows_test e2e_test)
    add_executable(${name} ${name}.cpp)
    target_compile_definitions(${name} PRIVATE ${TEST_DEFINITIONS})
    target_link_libraries(${name} PRIVATE Qt6::Test icloudphotos_objects)
    add_test(NAME ${name} COMMAND ${name})
endforeach()

# Every test gets throw-away config, data and cache folders (see testutils.h),
# so the user's digikamrc and engine session are never touched.

set_tests_properties(plugin_load_test digikam_loader_test engine_test windows_test e2e_test
                     PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 120)
