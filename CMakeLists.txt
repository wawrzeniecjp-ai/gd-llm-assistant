cmake_minimum_required(VERSION 3.29)

project(gd_ai_assistant VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Arc ships only its Core and Time features for mod projects (the SDK's top-level
# CMakeLists forces ARC_FEATURE_FULL OFF when this project is not top-level).
# Opt into the full feature set -- net, signal, debug -- BEFORE the SDK is added,
# so that arc::TcpStream / arc::UdpSocket are available for the WebSocket bridge.
# See SPEC.md section 6.1.9.
set(ARC_FEATURE_FULL ON CACHE BOOL "" FORCE)

# Pulls in the Geode SDK from $GEODE_SDK. Requires GEODE_SDK to be set in the
# environment (workspace-scoped in .vscode/settings.json).
add_subdirectory($ENV{GEODE_SDK} ${CMAKE_CURRENT_BINARY_DIR}/geode)

add_library(${PROJECT_NAME} SHARED
    src/main.cpp
    src/bridge/bridge.cpp
    src/editor/camera.cpp
    src/editor/color_channel.cpp
    src/editor/editor_state.cpp
    src/editor/level_revision.cpp
    src/editor/level_settings.cpp
    src/editor/object_catalog.cpp
    src/editor/properties.cpp
    src/editor/save_properties.cpp
    src/editor/screenshot.cpp
    src/editor/transaction.cpp
    src/exec/design_jobs.cpp
    src/exec/replay.cpp
    src/exec/editor_jobs.cpp
    src/exec/handler_verification.cpp
    src/exec/job_queue.cpp
    src/net/sha1.cpp
    src/net/websocket.cpp
    src/ui/chat_panel.cpp
    src/ui/panel_host.cpp
)

setup_geode_mod(${PROJECT_NAME})
