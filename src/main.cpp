#include <Geode/Geode.hpp>
#include <Geode/modify/LevelEditorLayer.hpp>
#include <Geode/ui/GeodeUI.hpp>

#include <chrono>

#include "bridge/bridge.hpp"
#include "editor/transaction.hpp"
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

/// Object count as last reported to the sidecar, or -1 before the first look.
///
/// Used to notice edits made **by hand**, so the assistant can be told its picture
/// of the level is out of date. Spike S4 (proper change detection) was never done,
/// and hooking the editor's mutation paths is invasive, so this uses the poll the
/// drain already performs: one `CCArray::count()` per frame, which is cheap.
///
/// It catches structural changes -- creations and deletions -- and not a bare move
/// or rotate, which is the right trade for now: the stale-context problem that
/// actually hurts is the model believing objects exist that the user deleted.
int g_reportedObjectCount = -1;

/// When the last `level_changed` was sent, in milliseconds.
///
/// Coalescing per SPEC.md 6.1.7 (at most one every 250 ms). A one-shot flag was
/// tried first and lost information: add two objects, delete them again, and the
/// deletion was swallowed because the flag was still set, so the sidecar never
/// heard about it. Comparing against the last *reported* count and rate-limiting
/// instead means a later edit is always reported eventually.
double g_lastLevelChangeMs = 0.0;
constexpr double kLevelChangeCoalesceMs = 250.0;

/// Monotonic milliseconds, for the coalescing window.
///
/// `steady_clock` rather than wall time: this measures an interval, and the only
/// thing that matters is that it never goes backwards.
double monotonicMs() {
    using namespace std::chrono;
    return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count();
}

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
            g_reportedObjectCount = -1;
            gdai::ui::PanelHost::get().detach();
            return;
        }
        gdai::ui::PanelHost::get().attach(editor);
        gdai::exec::JobQueue::get().drain(frameBudgetMs());

        // Notice hand edits.
        //
        // The baseline is updated on *every* frame, including while a transaction is
        // open. Skipping it then would leave the baseline at its pre-edit value, and
        // the turn's own edits would be reported as the user's the moment the
        // transaction closed -- exactly the opposite of what this is for. So a
        // transaction absorbs its changes silently instead of suppressing the update.
        if (auto* objects = editor->getAllObjects()) {
            auto const count = static_cast<int>(objects->count());
            auto const ours = gdai::editor::TransactionManager::get().isOpen();

            if (g_reportedObjectCount < 0 || ours) {
                g_reportedObjectCount = count;
            } else if (count != g_reportedObjectCount) {
                auto const nowMs = monotonicMs();
                if (nowMs - g_lastLevelChangeMs >= kLevelChangeCoalesceMs) {
                    gdai::bridge::Bridge::get().sendLevelChanged(
                        count - g_reportedObjectCount
                    );
                    g_lastLevelChangeMs = nowMs;
                    g_reportedObjectCount = count;
                }
            }
        }

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
