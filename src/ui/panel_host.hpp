#pragma once

#include <Geode/Geode.hpp>

#include <string>
#include <string_view>
#include <optional>
#include <vector>

#include "chat_panel.hpp"

namespace gdai::ui {

/// Owns the chat panel's lifetime and is the only supported way to reach it from
/// off the main thread.
///
/// The two sides of this class exist because they run on different threads. The
/// panel is Cocos, so it may only be touched on the main thread. The bridge runs
/// on the Arc runtime, so it may not. Every `post*` method here is callable from
/// any thread and hops to the main thread itself; `attach`/`detach` are main
/// thread only, which is where the editor lifecycle already runs.
///
/// The submit and cancel handlers are stored rather than forwarded, so they can
/// be installed before any editor is open -- which is what happens, since the
/// mod's `Loaded` event fires long before the first level is opened.
class PanelHost {
public:
    static PanelHost& get();

    // -- main thread only ------------------------------------------------

    /// Creates and attaches the panel, or returns the existing one.
    ///
    /// Returns null while `EditorUI` does not exist yet, so callers should treat
    /// null as "try again next frame" rather than as an error.
    ChatPanel* attach(LevelEditorLayer* editor);

    /// Destroys the panel. Called when the editor closes; the panel is owned by
    /// the editor's node tree, so leaving it alive would outlive its parent.
    void detach();

    // -- any thread ------------------------------------------------------

    void postStatus(Status status);

    /// A system line: connection changes, "reconnecting", and similar.
    void postSystem(std::string text);

    void postError(std::string text);

    /// Echoes the user's own message into the transcript.
    ///
    /// Rendered locally rather than waiting for the sidecar to echo it back:
    /// the user's message is not level content, so there is nothing to wait for,
    /// and a message that appears only after a round trip reads as lag.
    void postUserMessage(std::string text);

    void postAssistantMessage(std::string const& messageId, std::string text);
    void postAssistantDelta(std::string const& messageId, std::string delta);

    /// A turn started: show the thinking indicator and turn Send into Stop.
    void postTurnStarted();

    /// A turn ended: hide the indicator, restore Send, and show the summary.
    void postTurnFinished(std::string status, std::vector<std::string> lines);

    /// Clears the transcript, as when the sidecar pushes history and it is
    /// rebuilt from scratch.
    void postClear();

    void setSubmitHandler(geode::Function<void(std::string)> onSubmit);
    void setCancelHandler(geode::Function<void()> onCancel);

    /// True when a panel is currently attached. Main thread only.
    [[nodiscard]] bool hasPanel() const {
        return m_panel != nullptr;
    }

private:
    PanelHost() = default;

    /// Runs `fn` against the panel on the main thread, if one is attached by the
    /// time it runs. Safe to call from any thread.
    ///
    /// `fn` must be invoked non-const, because `geode::Function` is a
    /// `std::move_only_function` and its call operator is non-const. Taking it by
    /// value and marking the capturing lambda `mutable` is the way to satisfy
    /// that; capturing by reference would dangle, since the lambda outlives the
    /// call that created it.
    void onPanel(geode::Function<void(ChatPanel&)> fn);

    /// The attached panel, or null. Only ever read or written on the main thread.
    ChatPanel* m_panel = nullptr;

    /// The most recent connection state and system line.
    ///
    /// Remembered because the handshake routinely completes **before** the panel
    /// exists: the bridge connects from the editor's `init`, while the panel
    /// cannot be built until `EditorUI` has been assigned. Posting to a panelless
    /// host therefore silently discarded the "Connected." line and the status the
    /// dot needs, so the panel opened looking disconnected while the bridge was
    /// connected and answering turns.
    ///
    /// Main thread only. The values are written inside the `queueInMainThread`
    /// hop rather than by the calling thread, so there is no lock here and none
    /// is needed.
    std::optional<Status> m_lastStatus;
    std::string m_lastSystemText;

    geode::Function<void(std::string)> m_onSubmit;
    geode::Function<void()> m_onCancel;
};

} // namespace gdai::ui
