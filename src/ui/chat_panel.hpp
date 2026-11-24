#pragma once

#include <Geode/Geode.hpp>

#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace gdai::ui {

/// Connection state, shown as the header dot (SPEC.md 8.8).
enum class Status {
    Disconnected,
    Reconnecting,
    NeedsSetup,
    Ready,
};

/// The non-modal assistant panel (SPEC.md 8, decision D8).
///
/// Deliberately **not** a `geode::Popup`: that derives from the alert-layer
/// family and is modal with a dimming overlay, which would block the editor and
/// defeat the point of live visual feedback. This is a plain `CCLayer` child of
/// the editor UI, so the user keeps editing while it is visible.
///
/// Input routing (spike S6) is narrower than it first seemed.
///
/// The panel registers as a targeted touch delegate and claims **only the message
/// area** -- the one region with no interactive children of its own. The reason is
/// not to stop the editor reacting to taps in the panel; it is that a drag the user
/// means as a scroll otherwise *also* reaches the level behind the panel and panes
/// the editor at the same time. Claiming the area removes it from the editor's
/// view entirely.
///
/// Claiming the message area does not take the scroll gesture away from the scroll
/// layer, because the scroll layer's own touch priority is ahead of the panel's: it
/// sees the touch first and handles it, and the panel's claim only catches what is
/// left. Claiming the *whole* panel would be wrong for a different reason -- it
/// swallows the touches that belong to the send button and the text input, which is
/// how the send button came to look broken.
///
/// A click on the panel's empty margin is not claimed and does reach the editor.
/// That is a cosmetic annoyance and not worth claiming a region the panel does not
/// own.
/// Keyboard: Enter submits (SPEC.md 8.5).
///
/// Geode v5's `TextInput` wrapped a `CCTextInputNode` that never forwarded
/// `enterPressed` or `textInputReturn`, so Enter had to be caught by taking over
/// that node's delegate slot (`InputForwarder`) and, failing that, by hooking
/// `CCTextInputNode::onTextFieldInsertText` and `EditorUI::keyDown`. Registering
/// the panel as a keyboard delegate never worked at all: GD suppresses
/// `CCKeyboardDispatcher` dispatch while a text field has focus.
///
/// **v6 removed all of it.** `TextInput` was rewritten, it no longer wraps a
/// `CCTextInputNode`, and `TextInputDelegate` is gone -- so `InputForwarder`
/// could not even be declared. What replaced it is `TextInput::setEnterCallback`,
/// which is the supported way to see Enter and the only one now used.
///
/// `inputHasFocus` survives because playtest suppression still needs it, but it
/// is a plain flag fed by `setFocusCallback` rather than a field on a node we no
/// longer own. `EditorUI::keyDown` still calls submit as a belt-and-braces
/// fallback; submitting an empty draft is a no-op, so a second call is harmless.
class ChatPanel final : public cocos2d::CCLayer {
public:
    static ChatPanel* create();

    bool init() override;

    void onExit() override;

    /// True when `node` is this panel's own text input.
    ///
    /// The hook is global -- it fires for every text field in the game -- so it has
    /// to ask whether the node belongs to us. Only one panel exists at a time.
    /// Submits the draft, for the `EditorUI::keyDown` fallback to call on Enter.
    static void submitFromInputNode();

    /// True when the panel is open and its text input has keyboard focus.
    [[nodiscard]] static bool inputHasFocus();

    /// Lifts the post-send playtest block, for a playtest the assistant starts itself
    /// (`run_replay`): the block exists to stop the user's Enter from starting one.
    static void clearPlaytestSuppression();

    /// Shows or hides the message area and input, keeping the header visible.
    void toggle();

    [[nodiscard]] bool isExpanded() const {
        return m_expanded;
    }

    /// The attached panel, or null. For hooks that have no `this` of their own.
    [[nodiscard]] static ChatPanel* attached();

    /// User-level hide: the panel is not drawn at all.
    void setHidden(bool hidden);
    [[nodiscard]] bool isHidden() const {
        return m_hidden;
    }

    /// Playtest-level hide, deliberately separate from the user's choice.
    ///
    /// Playtesting needs the panel out of the way, but it must come back exactly as
    /// the user left it -- including not reappearing if they had hidden it themselves.
    /// One flag shared between the two would lose that.
    void setPlaytestHidden(bool hidden);

    /// Scrolls the transcript by `y` **if the cursor is over it**.
    ///
    /// Returns true when the wheel event was consumed, which is what stops it also
    /// scrolling the level behind the panel.
    bool handleScroll(float y);

    /// Updates the header dot (SPEC.md 8.8).
    void setStatus(Status status);

    /// Appends a message. `role` selects the styling per SPEC.md 8.4.
    void addMessage(std::string_view role, std::string const& text);

    /// Appends, or replaces the text of, the message identified by `id`.
    ///
    /// The protocol can send a message's text more than once per turn
    /// (`assistant_message` after deltas), so rendering has to be idempotent
    /// rather than append-only. An empty `id` always appends, which is what
    /// locally generated messages want.
    void upsertMessage(
        std::string const& id, std::string_view role, std::string const& text
    );

    /// Appends `delta` to the text currently held for `id`.
    void appendMessageDelta(std::string const& id, std::string const& delta);

    /// Removes every message. Used when the sidecar pushes history and the
    /// transcript is rebuilt from scratch.
    void clearMessages();

    /// Shows or hides the animated thinking indicator (SPEC.md 8.9).
    void setThinking(bool on);

    /// Names the tool the turn is currently running, or clears it.
    ///
    /// Shown inside the thinking indicator instead of the bare word "Thinking". A
    /// long turn and a hung one look identical otherwise, so this is what tells the
    /// user the assistant is working rather than stuck -- and *what* it is doing, which
    /// is the difference between watching progress and waiting blindly.
    void setProgress(std::string toolName);

    /// Replaces the thinking indicator with a turn summary (SPEC.md 8.9).
    ///
    /// `txnId` is the transaction the turn opened, or empty when it changed
    /// nothing. A non-empty id reveals the header's **Revert** action, which is the
    /// only way back from a turn's edits: they never reach the editor's undo stack,
    /// so Ctrl+Z undoes the user's own last edit instead of ours (D2).
    void showTurnSummary(
        std::string const& status,
        std::vector<std::string> const& lines,
        std::string const& txnId = {}
    );

    /// Reports the outcome of a revert the user asked for, and drops the action.
    ///
    /// Always drops it, whatever the outcome: a failed revert cannot become
    /// possible again without another turn, so leaving the button would invite
    /// the user into the same failure twice.
    void finishRevert(bool ok, std::string const& message);

    /// Enables or disables the input row. Disabled doubles as "processing", where
    /// the button becomes Stop (SPEC.md 8.5).
    void setInputEnabled(bool enabled);

    /// Shows the input button as Stop rather than Send.
    void setProcessing(bool processing);

    /// The current input text, trimmed.
    [[nodiscard]] std::string draft() const;

    /// Clears the input.
    void clearDraft();

    /// Callback fired when the user submits; empty text is never submitted.
    void setOnSubmit(geode::Function<void(std::string)> onSubmit);

    /// Callback fired when the user presses Stop mid-turn.
    void setOnCancel(geode::Function<void()> onCancel);

    /// Callback fired when the user presses **Revert** on a turn summary.
    ///
    /// The panel does not revert anything itself -- it does not know about the
    /// transaction manager -- so it hands over the id and waits to be told the
    /// outcome through `finishRevert`.
    void setOnRevert(geode::Function<void(std::string)> onRevert);

private:
    ChatPanel() = default;

    void buildBackground();
    void buildHeader();
    void buildMessages();
    void buildInput();
    void relayout();

    /// The height of the message area, derived from the expanded panel size.
    /// One definition, used by both the builder and the re-layout, so the two
    /// cannot drift apart.
    [[nodiscard]] float messageAreaHeight() const;

    /// The message area's rect in the panel's own space, used for hit testing.
    [[nodiscard]] cocos2d::CCRect messageAreaRect() const;

    /// The width text may occupy, in the label's unscaled units.
    [[nodiscard]] float usableTextWidth() const;

    // -- touch (SPEC.md 8.1) ---------------------------------------------

    void registerWithTouchDispatcher() override;

    bool ccTouchBegan(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;
    void ccTouchMoved(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;
    void ccTouchEnded(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;
    void ccTouchCancelled(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;

    void onSubmit(CCObject* sender);
    void onToggleExpanded(CCObject* sender);
    void submitDraft();
    void appendMessageNode(cocos2d::CCNode* node);
    void scrollToBottom();

    /// The user pressed Revert: disable the action and forward the id.
    void requestRevert(std::string txnId);

    /// Removes the Revert action, if one is on screen.
    ///
    /// Only the newest turn keeps its action: reverting an older one would apply
    /// its inverse on top of changes that assume the newer turn's edits are still
    /// present, which is what `TRANSACTION_NOT_REVERTIBLE` exists to prevent.
    void dropRevertAction();

    /// Creates the label node for a message, applying role styling and, for
    /// untrusted roles, the sanitiser (SPEC.md 8.3.1).
    geode::Label* makeMessageLabel(std::string_view role, std::string const& text);

    /// Recomputes the label's height for wrapped text and re-runs the layout.
    void refreshLayout();

    void tickThinking(float dt);

    /// Rewrites the wrapped draft preview to match the input box.
    ///
    /// `TextInput` is single-line and shrinks its own label to fit, so a long message
    /// becomes unreadable while it is being typed and cannot be broken across lines.
    /// The preview mirrors the draft above the input at a fixed size, shown only when
    /// the draft no longer fits on one line -- so a short message looks exactly as it
    /// did before.
    void updateDraftPreview();

    /// The rect the draft preview occupies, empty when it is hidden.
    [[nodiscard]] cocos2d::CCRect draftPreviewRect() const;

    /// The input row's rect, in the panel's own space.
    [[nodiscard]] cocos2d::CCRect inputRowRect() const;

    /// Applies `m_hidden` and `m_playtestHidden` to the node's visibility.
    void applyVisibility();

    /// Reserved id of the thinking indicator, so it can never collide with a
    /// protocol message id (those are server-generated).
    static constexpr std::string_view kThinkingId = "__thinking__";

    bool m_expanded = true;
    bool m_processing = false;

    /// Hidden by the user, via the hide keybind.
    bool m_hidden = false;

    /// Hidden because the player is testing the level.
    bool m_playtestHidden = false;
    Status m_status = Status::Disconnected;

    cocos2d::CCNode* m_background = nullptr;
    cocos2d::CCNode* m_header = nullptr;

    /// A flat colour quad rather than a sprite.
    ///
    /// The sprite version was invisible, and the reason is worth keeping: a sprite
    /// is tinted by *multiplying* `setColor` with its texture, so tinting a dark
    /// frame green yields black. `square02_001.png` is a dark frame, so the "dot"
    /// was drawn as a black square on a near-black panel. A `CCLayerColor` has no
    /// texture to multiply against and cannot fail this way.
    cocos2d::CCLayerColor* m_statusDot = nullptr;
    geode::ScrollLayer* m_messages = nullptr;

    /// GD declares `CCContentLayer` in the global namespace, not in `cocos2d`
    /// (unlike the cocos2d-x classes it derives from). Qualifying it compiles to
    /// a hard error that then cascades into "cannot convert ChatPanel" noise for
    /// every subsequent use of the class, which is worth knowing when reading
    /// this build's diagnostics.
    CCContentLayer* m_messageContent = nullptr;
    cocos2d::CCNode* m_inputRow = nullptr;
    geode::TextInput* m_input = nullptr;

    /// Wrapped mirror of the draft, shown only when it does not fit on one line.
    geode::Label* m_draftPreview = nullptr;

    /// Height reserved for the preview, or 0 when it is hidden.
    float m_draftPreviewHeight = 0.f;

    /// The tool the running turn is using, or empty. Shown by the thinking indicator.
    std::string m_progressTool;
    cocos2d::CCLabelBMFont* m_buttonLabel = nullptr;
    geode::Button* m_actionButton = nullptr;
    geode::Label* m_emptyHint = nullptr;

    /// Messages currently in the list, oldest first (SPEC.md 8.10 caps this).
    std::vector<cocos2d::CCNode*> m_messageNodes;

    /// Message id -> label and accumulated text, for in-place updates.
    std::unordered_map<std::string, geode::Label*> m_labelById;
    std::unordered_map<std::string, std::string> m_textById;

    /// Order in which ids were first seen, so eviction under the history cap can
    /// drop the corresponding map entries.
    std::vector<std::string> m_idOrder;

    /// The thinking indicator's node, or null when no turn is running.
    ///
    /// `cocos2d::CCLabelBMFont` rather than `geode::Label`: unlike
    /// `CCContentLayer`, this one *is* namespaced, and the thinking indicator is
    /// plain ASCII that does not need the richer renderer.
    cocos2d::CCLabelBMFont* m_thinkingNode = nullptr;
    int m_thinkingStep = 0;

    /// Panel size when expanded, in points. The collapsed size is derived.
    cocos2d::CCSize m_size { 216.f, 196.f };

    /// Set while this panel is attached, so global hooks can find their panel.
    /// Exactly one panel exists at a time.
    static ChatPanel* s_attached;

    /// Whether the text input currently holds keyboard focus.
    ///
    /// Fed by `TextInput::setFocusCallback`. It used to be read off the input
    /// node's `m_selected` field, which v6 removed along with the rest of the
    /// `CCTextInputNode` wrapper -- a callback is the supported source anyway.
    bool m_inputFocused = false;

    geode::Function<void(std::string)> m_onSubmit;
    geode::Function<void()> m_onCancel;
    geode::Function<void(std::string)> m_onRevert;

    /// The Revert action on the header, or null. There is exactly one.
    ///
    /// In the header rather than on the summary bubble: see `buildHeader`.
    geode::Button* m_revertButton = nullptr;

    /// The transaction the visible Revert action would undo, or empty.
    std::string m_revertTxnId;
};

} // namespace gdai::ui
