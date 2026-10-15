#include <Geode/Geode.hpp>
#include <Geode/modify/LevelEditorLayer.hpp>

#include "bridge/bridge.hpp"
#include "net/sha1.hpp"

using namespace geode::prelude;

namespace {

/// SPEC.md 7.8: the mod connects when the editor opens and closes the socket
/// when it closes. `auto-connect` (SPEC.md 6.1.8) gates the automatic path;
/// without it the user drives the connection from the panel, which is Phase 3.
bool autoConnectEnabled() {
    return Mod::get()->getSettingValue<bool>("auto-connect");
}

} // namespace

class $modify(EditorBridgeHook, LevelEditorLayer) {
    bool init(GJGameLevel* level, bool noUI) {
        if (!LevelEditorLayer::init(level, noUI)) {
            return false;
        }
        if (autoConnectEnabled()) {
            gdai::bridge::Bridge::get().start();
        }
        return true;
    }

    // NOTE: there is deliberately no `onExit` hook here.
    // `LevelEditorLayer` does not declare `onExit` in the Geode bindings (unlike
    // `PlayLayer` and most other layers), so a `$modify` hook on it compiles but
    // never fires. Editor close is detected by polling, and game exit by the
    // `Exiting` event below; see gdai::bridge::Bridge::onGameExiting.
};

/// Fired when the game is about to exit, before static destructors run.
///
/// This is the reliable shutdown signal: without it the session task kept
/// polling, and Geode's async-runtime shutdown blocked on it, which showed up as
/// a 5-10 second hang on exit.
$on_game(Exiting) {
    gdai::bridge::Bridge::get().onGameExiting();
}

$on_mod(Loaded) {
    log::info("GD AI Assistant {} loaded", Mod::get()->getVersion().toVString());

    // The WebSocket handshake depends on SHA-1, which neither Geode nor Arc
    // provides. Refuse to connect on a bad digest rather than handshaking with
    // a wrong one.
    if (!gdai::net::sha1SelfTest()) {
        log::error("SHA-1 self-test FAILED; the bridge will not connect");
        return;
    }
    log::info("SHA-1 self-test passed");
}
