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

/// Text-input callbacks chained to the delegate `geode::TextInput` installs on
/// the input node, intercepting Enter on the way past.
///
/// Taking the slot is necessary (see the class comment above) and chaining is
/// what makes it safe: `TextInput`'s own behaviour -- its internal string, its
/// placeholder, its width handling -- is driven by `textChanged`, so dropping
/// its delegate would quietly break the widget rather than fail loudly.
///
/// Enter fires the callback from both `enterPressed` and `textInputReturn`,
/// because which one GD's node calls is not documented. That is safe because
/// submitting clears the draft, so the second call finds nothing to send.
class InputForwarder final : public TextInputDelegate {
public:
    void setInner(TextInputDelegate* inner) {
        m_inner = inner;
    }

    void setOnEnter(geode::Function<void()> onEnter) {
        m_onEnter = std::move(onEnter);
    }

    void textChanged(CCTextInputNode* node) override;
    void textInputOpened(CCTextInputNode* node) override;
    void textInputClosed(CCTextInputNode* node) override;
    void textInputShouldOffset(CCTextInputNode* node, float yOffset) override;
    bool allowTextInput(CCTextInputNode* node) override;
    void enterPressed(CCTextInputNode* node) override;
    void textInputReturn(CCTextInputNode* node) override;

private:
    TextInputDelegate* m_inner = nullptr;
    geode::Function<void()> m_onEnter;
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
/// `geode::TextInput` does not forward `enterPressed` or `textInputReturn` -- it
/// only forwards `textChanged` -- so Enter never reached the panel, which is why
/// clicking Send worked and Enter did nothing.
///
/// A keyboard delegate does not work either, and is worth recording as a dead
/// end: the panel was registered as one and its `keyDown` was never called. GD
/// suppresses `CCKeyboardDispatcher` dispatch while a text field has focus (so
/// the editor does not react to typing), which suppresses mod-registered
/// delegates along with everything else. Characters still reach the field because
/// they arrive by the IME path, not by that dispatcher.
///
/// That leaves taking over the input node's delegate slot, which is what
/// `InputForwarder` below is for.
/// Enter handling (SPEC.md 8.5), and the two approaches that do **not** work.
///
/// `geode::TextInput` does not forward `enterPressed` or `textInputReturn` -- it
/// only forwards `textChanged` -- and `CCTextInputNode` is not a keyboard delegate
/// and takes text through the IME path, so neither is called. Installing a chained
/// delegate on the node therefore changes nothing.
///
/// Registering the panel as a keyboard delegate does not work either, and this was
/// measured rather than assumed: with `addDelegate`, with `forceAddDelegate`, and
/// with `CCLayer::setKeyboardEnabled(true)`, not one `keyDown` was logged while the
/// user was typing. GD does not dispatch keyboard messages while a text field holds
/// focus.
///
/// What is left is the hook below, on the node's own text-insertion callback. Its
/// `keyCodes` parameter is the only place GD surfaces the key that produced the
/// input, so it is the only place that can see Enter.
class ChatPanel final : public cocos2d::CCLayer, public TextInputDelegate {
public:
    static ChatPanel* create();

    bool init() override;

    void onExit() override;

    /// True when `node` is this panel's own text input.
    ///
    /// The hook is global -- it fires for every text field in the game -- so it has
    /// to ask whether the node belongs to us. Only one panel exists at a time.
    [[nodiscard]] static bool ownsInputNode(CCTextInputNode* node);

    /// Submits the draft, for the hook to call when it sees Enter.
    static void submitFromInputNode();

    /// True when the panel is open and its text input has keyboard focus.
    [[nodiscard]] static bool inputHasFocus();

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

    // -- TextInputDelegate: Enter submits (SPEC.md 8.5) ------------------

    void textInputReturn(CCTextInputNode* node) override;

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
    InputForwarder m_inputForwarder;
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

    /// Set while this panel is attached, so the global input hook can find its
    /// panel. Exactly one panel exists at a time.
    static ChatPanel* s_attached;

    /// The panel's own input node, cached so the hook can compare pointers.
    CCTextInputNode* m_inputNode = nullptr;

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
