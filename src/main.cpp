#include <Geode/Geode.hpp>
#include <Geode/modify/LevelEditorLayer.hpp>
#include <Geode/ui/GeodeUI.hpp>

#include "bridge/bridge.hpp"
#include "exec/job_queue.hpp"
#include "net/sha1.hpp"
#include "ui/chat_panel.hpp"

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

/// The panel for the currently open editor, or null if none is attached.
///
/// Deliberately a raw pointer held for exactly as long as the editor is open:
/// the panel is owned by the editor UI node, so letting it outlive the editor
/// would dangle. `detachPanel` is called from the same place that notices the
/// editor is gone.
gdai::ui::ChatPanel* g_panel = nullptr;

void armDrain();

/// Creates the panel once the editor UI exists, and returns it.
///
/// The panel cannot be built in `LevelEditorLayer::init` because `m_editorUI` is
/// not assigned yet at that point, so this is polled from the drain loop until it
/// becomes available.
gdai::ui::ChatPanel* attachPanel(LevelEditorLayer* editor) {
    if (g_panel) {
        return g_panel;
    }
    if (!editor || !editor->m_editorUI) {
        return nullptr;
    }

    auto panel = gdai::ui::ChatPanel::create();
    if (!panel) {
        log::error("panel: creation failed");
        return nullptr;
    }

    auto winSize = cocos2d::CCDirector::sharedDirector()->getWinSize();
    panel->setPosition({
        winSize.width - panel->getContentSize().width - 8.f,
        8.f,
    });

    // Above the editor's own chrome so its controls are reachable, but the
    // panel's touch priority (not this z-order) is what actually decides routing.
    editor->m_editorUI->addChild(panel, 1000);
    g_panel = panel;

    log::info(
        "panel: attached to editor UI at ({}, {}), win size {}x{}",
        panel->getPositionX(), panel->getPositionY(), winSize.width, winSize.height
    );
    return g_panel;
}

void detachPanel() {
    if (!g_panel) return;
    g_panel->removeFromParent();
    g_panel = nullptr;
    log::info("panel: detached");
}

void togglePanel() {
    if (g_panel) {
        g_panel->toggle();
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
            detachPanel();
            return;
        }
        attachPanel(editor);
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
    detachPanel();
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

    log::info("panel: listening for the 'toggle-chat' keybind");
}
