#pragma once

#include <Geode/Geode.hpp>

#include <string>
#include <string_view>
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
/// Input routing is what makes that work (spike S6):
/// - Touches inside the panel are consumed; touches outside are **not**, so the
///   editor keeps receiving them. `ccTouchBegan` returning false for an outside
///   touch is what lets it fall through to the editor.
/// - The panel sits at a high z-order so its own controls receive input first.
///
/// Keyboard focus is the remaining S6 question: while the text input is focused,
/// typed characters must not reach editor hotkeys. GD's `CCTextInputNode` takes
/// over keyboard handling when focused, but that is unverified here and is logged
/// as a follow-up rather than assumed.
class ChatPanel final : public cocos2d::CCLayer, public TextInputDelegate {
public:
    static ChatPanel* create();

    bool init() override;

    /// Shows or hides the message area and input, keeping the header visible.
    void toggle();

    [[nodiscard]] bool isExpanded() const {
        return m_expanded;
    }

    /// Updates the header dot (SPEC.md 8.8).
    void setStatus(Status status);

    /// Appends a message. `role` selects the styling per SPEC.md 8.4.
    void addMessage(std::string_view role, std::string const& text);

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

    // -- TextInputDelegate: Enter submits (SPEC.md 8.5) ------------------

    void textInputReturn(CCTextInputNode* node) override;

private:
    ChatPanel() = default;

    void buildBackground();
    void buildHeader();
    void buildMessages();
    void buildInput();
    void relayout();

    void onSubmit(CCObject* sender);
    void onToggleExpanded(CCObject* sender);
    void submitDraft();
    void appendMessageNode(cocos2d::CCNode* node);
    void scrollToBottom();

    // -- touch routing (spike S6) ---------------------------------------

    bool ccTouchBegan(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;
    void ccTouchMoved(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;
    void ccTouchEnded(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;
    void ccTouchCancelled(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) override;
    void registerWithTouchDispatcher() override;

    /// True when a point (in this node's space) is over the panel body.
    [[nodiscard]] bool containsPoint(cocos2d::CCPoint const& point) const;

    bool m_expanded = true;
    bool m_processing = false;
    Status m_status = Status::Disconnected;

    cocos2d::CCNode* m_background = nullptr;
    cocos2d::CCNode* m_header = nullptr;
    cocos2d::CCSprite* m_statusDot = nullptr;
    geode::ScrollLayer* m_messages = nullptr;

    /// GD declares `CCContentLayer` in the global namespace, not in `cocos2d`
    /// (unlike the cocos2d-x classes it derives from). Qualifying it compiles to
    /// a hard error that then cascades into "cannot convert ChatPanel" noise for
    /// every subsequent use of the class, which is worth knowing when reading
    /// this build's diagnostics.
    CCContentLayer* m_messageContent = nullptr;
    cocos2d::CCNode* m_inputRow = nullptr;
    geode::TextInput* m_input = nullptr;
    cocos2d::CCLabelBMFont* m_buttonLabel = nullptr;
    geode::Button* m_actionButton = nullptr;
    geode::Label* m_emptyHint = nullptr;

    /// Messages currently in the list, oldest first (SPEC.md 8.10 caps this).
    std::vector<cocos2d::CCNode*> m_messageNodes;

    /// Panel size when expanded, in points. The collapsed size is derived.
    cocos2d::CCSize m_size { 240.f, 220.f };

    geode::Function<void(std::string)> m_onSubmit;
};

} // namespace gdai::ui
