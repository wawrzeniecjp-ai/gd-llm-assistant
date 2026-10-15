#include <Geode/Geode.hpp>
#include <Geode/modify/LevelEditorLayer.hpp>
#include <Geode/ui/GeodeUI.hpp>

#include "bridge/bridge.hpp"
#include "exec/job_queue.hpp"
#include "net/sha1.hpp"
#include "ui/panel_host.hpp"

using namespace geode::prelude;

namespace {

/// SPEC.md 7.8: the mod connects when the editor opens and closes the socket
/// when it closes. `auto-connect` (SPEC.md 6.1.8) gates the automatic path;
/// without it the user drives the connection from the panel, which is Phase 3.
bool autoConnectEnabled() {
    return Mod::get()->getSettingValue<bool>("auto-connect");
}

/// Per-frame main-thread edit budget (SPEC.md 6.1.4).
double frameBudgetMs() {
    return static_cast<double>(Mod::get()->getSettingValue<int64_t>("frame-budget-ms"));
}

/// True while the drain loop should keep re-arming itself.
bool g_draining = false;

void armDrain();

void togglePanel() {
    if (auto panel = gdai::ui::PanelHost::get().attach(LevelEditorLayer::get())) {
        panel->toggle();
    } else {
        log::warn("panel: toggle requested with no editor open");
    }
}

/// Runs one drain, then re-arms for the next frame.
///
/// The drain runs from Geode's **main-thread queue**, not from a cocos scheduled
/// selector on the editor layer. That distinction turned out to matter: removing
/// an object from inside a scheduled layer update -- i.e. while the editor may be
/// mid-traversal of its own object list -- crashed the game, whereas the same
/// `removeObject` call succeeds from the main-thread queue (which is where
/// `runTransactionProbe` performs it successfully).
///
/// Geode flushes `queueInMainThread` once per frame, so re-arming here gives a
/// per-frame cadence without depending on which update virtuals the editor
/// happens to call -- which was the other problem, since hooking
/// `GJBaseGameLayer::update` and `LevelEditorLayer::postUpdate` both installed
/// and neither was ever invoked.
///
/// The same loop attaches the panel, because `LevelEditorLayer::init` runs
/// before `m_editorUI` is assigned and so cannot do it.
void armDrain() {
    if (!g_draining) {
        return;
    }
    geode::queueInMainThread([] {
        if (!g_draining) {
            return;
        }
        // The editor closing ends the loop; nothing left to drain work for.
        auto editor = LevelEditorLayer::get();
        if (editor == nullptr) {
            g_draining = false;
            gdai::ui::PanelHost::get().detach();
            return;
        }
        gdai::ui::PanelHost::get().attach(editor);
        gdai::exec::JobQueue::get().drain(frameBudgetMs());
        armDrain();
    });
}

} // namespace

class $modify(EditorBridgeHook, LevelEditorLayer) {
    bool init(GJGameLevel* level, bool noUI) {
        if (!LevelEditorLayer::init(level, noUI)) {
            return false;
        }

        // The job queue's drain is driven by Geode's main-thread queue rather
        // than by a hooked virtual.
        //
        // Two problems led here, both worth recording:
        //
        //   1. Hooking failed, twice over. `LevelEditorLayer` does not declare
        //      `update` (it inherits it from `GJBaseGameLayer`), and Geode only
        //      installs hooks for functions declared in the target class's own
        //      bindings, so that hook was never installed. Then hooking
        //      `GJBaseGameLayer::update` and `LevelEditorLayer::postUpdate`
        //      installed both -- the log shows "Enabled ... hook" -- yet neither
        //      was invoked while the editor was open. So "check the log for the
        //      Enabled line" is necessary but not sufficient.
        //
        //   2. A cocos scheduled selector did run, but removing an object from
        //      inside a scheduled layer update crashed the game. The same
        //      `removeObject` call succeeds from the main-thread queue, which is
        //      where `runTransactionProbe` performs it.
        //
        // Draining from the main-thread queue satisfies both: it runs every
        // frame, and it mutates the editor from the context known to be safe.
        g_draining = true;
        armDrain();

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
    gdai::ui::PanelHost::get().detach();
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

    // Toggle the panel (SPEC.md 8.6). Fires on the game's input thread, which is
    // the main thread, so touching the scene graph here is safe. `repeat` is
    // filtered so holding the key does not flicker the panel.
    listenForKeybindSettingPresses(
        "toggle-chat",
        [](Keybind const&, bool down, bool repeat, double) {
            if (down && !repeat) {
                togglePanel();
            }
        }
    );

    // The panel and the bridge do not know about each other: this is the only
    // place they are wired, which keeps the UI free of transport details and the
    // bridge free of Cocos. Registered here rather than at panel attach because
    // this event fires long before any editor is open.
    gdai::ui::PanelHost::get().setSubmitHandler([](std::string text) {
        gdai::bridge::Bridge::get().sendUserMessage(std::move(text));
    });
    gdai::ui::PanelHost::get().setCancelHandler([] {
        gdai::bridge::Bridge::get().sendCancelTurn();
    });

    log::info("panel: listening for the 'toggle-chat' keybind");
}
