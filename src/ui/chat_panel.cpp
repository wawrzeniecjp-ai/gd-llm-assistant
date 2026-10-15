#include "chat_panel.hpp"

#include <Geode/ui/Button.hpp>
#include <Geode/ui/Label.hpp>
#include <Geode/ui/NineSlice.hpp>
#include <Geode/ui/ScrollLayer.hpp>
#include <Geode/ui/TextInput.hpp>
#include <Geode/utils/cocos.hpp>

#include <algorithm>
#include <array>

using namespace geode::prelude;

namespace gdai::ui {

namespace {

constexpr float kPad = 6.f;
constexpr float kHeaderHeight = 22.f;
constexpr float kInputHeight = 26.f;
constexpr float kButtonWidth = 46.f;

/// Touch priority (spike S6).
///
/// Lower values are dispatched first. GD alerts/popups register around -500, so
/// staying just above that keeps dialogs on top of the panel while the panel
/// still beats the editor's own layer (registered at 0) and its menus.
constexpr int kTouchPriority = -499;

/// The panel body, minus the header, is the region that consumes touches. The
/// header itself is excluded because collapsing it is handled by its own button,
/// and letting the header swallow touches would make it a dead zone.
cocos2d::ccColor3B statusColor(Status status) {
    switch (status) {
        case Status::Ready:        return { 90, 220, 120 };
        case Status::Reconnecting: return { 240, 200, 80 };
        case Status::NeedsSetup:   return { 240, 140, 60 };
        case Status::Disconnected: return { 170, 170, 180 };
    }
    return { 255, 255, 255 };
}

/// Sprite frame names differ across GD versions and installations. Rather than
/// assume one exists, probe the cache and log the outcome -- a missing frame
/// renders as nothing at all, which is indistinguishable from "the panel never
/// attached" when read from a screenshot alone.
char const* pickFrame(std::initializer_list<char const*> candidates, char const* what) {
    for (auto name : candidates) {
        if (cocos2d::CCSpriteFrameCache::get()->spriteFrameByName(name)) {
            log::debug("panel: {} -> frame '{}'", what, name);
            return name;
        }
    }
    log::warn("panel: no usable sprite frame found for {}", what);
    return nullptr;
}

/// Text colour per message role (SPEC.md 8.4).
cocos2d::ccColor3B roleColor(std::string_view role) {
    if (role == "user")           return { 190, 220, 255 };
    if (role == "assistant")      return { 255, 255, 255 };
    if (role == "thinking")       return { 170, 170, 180 };
    if (role == "action-summary") return { 200, 235, 190 };
    if (role == "error")          return { 255, 160, 160 };
    if (role == "system")         return { 200, 200, 210 };
    return { 255, 255, 255 };
}

std::string trimmed(std::string s) {
    auto notSpace = [](unsigned char c) { return !std::isspace(c); };
    s.erase(s.begin(), std::find_if(s.begin(), s.end(), notSpace));
    s.erase(std::find_if(s.rbegin(), s.rend(), notSpace).base(), s.end());
    return s;
}

} // namespace

ChatPanel* ChatPanel::create() {
    auto panel = new ChatPanel();
    if (panel->init()) {
        panel->autorelease();
        return panel;
    }
    delete panel;
    return nullptr;
}

bool ChatPanel::init() {
    if (!CCLayer::init()) return false;

    // One-by-one so a touch is either ours or the editor's, never both.
    setTouchEnabled(true);
    setTouchMode(cocos2d::kCCTouchesOneByOne);
    setKeypadEnabled(false);
    setContentSize(m_size);

    buildBackground();
    buildHeader();
    buildMessages();
    buildInput();
    relayout();

    setStatus(Status::Disconnected);
    addMessage("system", "Not connected to the sidecar.");

    log::info("panel: created, size {}x{}", m_size.width, m_size.height);
    return true;
}

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

void ChatPanel::buildBackground() {
    auto frame = pickFrame(
        { "square02b_001.png", "square02c_001.png", "square02_001.png", "GJ_square01.png" },
        "background"
    );

    if (frame) {
        auto bg = NineSlice::createWithSpriteFrameName(frame, { 10.f, 10.f, 10.f, 10.f });
        if (bg) {
            bg->setAnchorPoint({ 0.f, 0.f });
            bg->setPosition({ 0.f, 0.f });
            bg->setColor({ 20, 22, 30 });
            bg->setOpacity(230);
            addChild(bg, -1);
            m_background = bg;
            return;
        }
    }

    // Fallback: a plain square so the panel is still legible if the frames above
    // are all absent. Ugly but visible, which is what matters for diagnosing.
    auto bg = cocos2d::CCLayerColor::create({ 20, 22, 30, 230 });
    addChild(bg, -1);
    m_background = bg;
}

void ChatPanel::buildHeader() {
    m_header = cocos2d::CCNode::create();
    addChild(m_header, 1);

    auto dotFrame = pickFrame(
        { "square02_001.png", "square02b_001.png", "circle.png" },
        "status dot"
    );
    if (dotFrame) {
        m_statusDot = cocos2d::CCSprite::createWithSpriteFrameName(dotFrame);
        if (m_statusDot) {
            m_statusDot->setScale(0.22f);
            m_header->addChild(m_statusDot, 1);
        }
    }

    auto title = Label::create("LLM Assistant", "chatFont.fnt");
    if (title) {
        title->setAnchorPoint({ 0.f, 0.5f });
        title->setScale(0.62f);
        title->setColor({ 235, 235, 245 });
        title->setID("title"_spr);
        m_header->addChild(title, 1);
    }

    // Collapse/expand. Uses a sprite rather than a label so its meaning does not
    // depend on the font containing a suitable glyph.
    auto collapse = Button::createWithSpriteFrameName("GJ_arrow_01.png", [this](Button*) {
        onToggleExpanded(nullptr);
    });
    if (collapse) {
        collapse->setScale(0.42f);
        collapse->setRotation(90.f); // right-pointing becomes down-pointing
        collapse->setID("collapse"_spr);
        m_header->addChild(collapse, 1);
    }
}

void ChatPanel::buildMessages() {
    auto width = m_size.width - 2.f * kPad;
    auto height = m_size.height - kHeaderHeight - kInputHeight - 4.f * kPad;

    m_messages = ScrollLayer::create({ width, height }, true, true);
    if (!m_messages) {
        log::error("panel: ScrollLayer::create returned null");
        return;
    }
    m_messages->setAnchorPoint({ 0.f, 0.f });
    m_messages->setID("messages"_spr);

    // Column layout with the axis reversed packs children downwards from the top,
    // which is what a chat transcript wants.
    if (auto content = m_messages->m_contentLayer) {
        content->setLayout(ScrollLayer::createDefaultListLayout(4.f));
        m_messageContent = content;
    } else {
        log::error("panel: ScrollLayer has no content layer");
    }

    addChild(m_messages, 1);

    m_emptyHint = Label::create("No messages yet.", "chatFont.fnt");
    if (m_emptyHint) {
        m_emptyHint->setScale(0.55f);
        m_emptyHint->setColor({ 150, 150, 160 });
        m_messages->addChild(m_emptyHint, 2);
    }
}

void ChatPanel::buildInput() {
    m_inputRow = cocos2d::CCNode::create();
    addChild(m_inputRow, 1);

    auto input = TextInput::create(100.f, "Ask about this level...", "chatFont.fnt");
    if (input) {
        input->setScale(0.62f);
        input->setMaxCharCount(2000); // matches the mod's max-message-length default
        input->setDelegate(this, 1);
        input->setID("input"_spr);
        m_inputRow->addChild(input, 1);
        m_input = input;
    }

    m_buttonLabel = cocos2d::CCLabelBMFont::create("Send", "chatFont.fnt");
    if (m_buttonLabel) {
        m_buttonLabel->setScale(0.6f);
    }

    auto button = Button::createWithSpriteFrameName("GJ_button_01.png", [this](Button* sender) {
        onSubmit(sender);
    });
    if (button) {
        button->setScale(0.55f);
        button->setID("send"_spr);
        if (m_buttonLabel) {
            button->addChild(m_buttonLabel, 2);
        }
        m_inputRow->addChild(button, 1);
        m_actionButton = button;
    }
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

void ChatPanel::relayout() {
    auto width = m_size.width;
    auto height = m_expanded ? m_size.height : kHeaderHeight + 2.f * kPad;

    setContentSize({ width, height });
    if (m_background) {
        m_background->setContentSize({ width, height });
    }

    auto headerCenterY = height - kPad - kHeaderHeight * 0.5f;
    if (m_header) {
        m_header->setPosition({ 0.f, headerCenterY - kHeaderHeight * 0.5f });
    }
    if (m_statusDot) {
        m_statusDot->setPosition({ kPad + 5.f, kHeaderHeight * 0.5f });
    }
    if (auto title = m_header ? m_header->getChildByID("title"_spr) : nullptr) {
        title->setPosition({ kPad + 13.f, kHeaderHeight * 0.5f });
    }
    if (auto collapse = m_header ? m_header->getChildByID("collapse"_spr) : nullptr) {
        collapse->setPosition({ width - kPad - 7.f, kHeaderHeight * 0.5f });
    }

    if (m_messages) {
        m_messages->setVisible(m_expanded);
        m_messages->setPosition({ kPad, kPad + kInputHeight + kPad });
        m_messages->setContentSize({
            width - 2.f * kPad,
            m_size.height - kHeaderHeight - kInputHeight - 4.f * kPad,
        });
    }

    if (m_inputRow) {
        m_inputRow->setVisible(m_expanded);
        m_inputRow->setPosition({ 0.f, 0.f });
    }
    auto inputWidth = width - 2.f * kPad - kButtonWidth - kPad;
    if (m_input) {
        m_input->setPosition({ kPad + inputWidth * 0.5f, kPad + kInputHeight * 0.5f });
        m_input->setWidth(inputWidth / 0.62f);
    }
    if (m_actionButton) {
        m_actionButton->setPosition({ width - kPad - kButtonWidth * 0.5f, kPad + kInputHeight * 0.5f });
    }

    scrollToBottom();
}

void ChatPanel::toggle() {
    m_expanded = !m_expanded;
    relayout();
    log::debug("panel: expanded = {}", m_expanded);
}

void ChatPanel::setStatus(Status status) {
    m_status = status;
    if (m_statusDot) {
        m_statusDot->setColor(statusColor(status));
    }
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

void ChatPanel::addMessage(std::string_view role, std::string const& text) {
    if (!m_messageContent) return;

    auto width = m_size.width - 2.f * kPad - 8.f;

    auto label = Label::create(text, "chatFont.fnt");
    if (!label) return;
    label->setAnchorPoint({ 0.f, 1.f });
    label->setScale(0.56f);
    label->setColor(roleColor(role));
    label->setMaxWidth(width / 0.56f);
    label->setAlignment(Label::Alignment::Left);

    appendMessageNode(label);
}

void ChatPanel::appendMessageNode(cocos2d::CCNode* node) {
    m_messageContent->addChild(node);
    m_messageNodes.push_back(node);

    if (m_emptyHint) {
        m_emptyHint->setVisible(false);
    }

    // SPEC.md 8.10: keep only the most recent messages.
    constexpr size_t kMaxMessages = 100;
    while (m_messageNodes.size() > kMaxMessages) {
        auto oldest = m_messageNodes.front();
        m_messageNodes.erase(m_messageNodes.begin());
        if (oldest && oldest->getParent()) {
            oldest->removeFromParent();
        }
    }

    m_messageContent->updateLayout();
    scrollToBottom();
}

void ChatPanel::scrollToBottom() {
    if (!m_messages || !m_messages->m_contentLayer) return;

    auto content = m_messages->m_contentLayer;
    auto contentHeight = content->getContentSize().height;
    auto viewHeight = m_messages->getContentSize().height;

    // A ColumnLayout with a reversed axis grows the content layer downwards from
    // the top, so the layer's own position stays at 0 and the newest item sits at
    // the bottom of the content. Scrolling to the bottom therefore means pushing
    // the content up by however much it exceeds the viewport.
    auto overflow = std::max(0.f, contentHeight - viewHeight);
    content->setPositionY(overflow);

    log::debug(
        "panel: scroll content={} view={} offset={}",
        contentHeight, viewHeight, overflow
    );
}

void ChatPanel::setInputEnabled(bool enabled) {
    if (m_input) m_input->setEnabled(enabled);
    if (m_actionButton) m_actionButton->setEnabled(enabled);
}

void ChatPanel::setProcessing(bool processing) {
    m_processing = processing;
    if (m_buttonLabel) {
        m_buttonLabel->setString(processing ? "Stop" : "Send");
    }
}

std::string ChatPanel::draft() const {
    if (!m_input) return {};
    return trimmed(std::string(m_input->getString()));
}

void ChatPanel::clearDraft() {
    if (m_input) m_input->setString("", false);
}

void ChatPanel::setOnSubmit(geode::Function<void(std::string)> onSubmit) {
    m_onSubmit = std::move(onSubmit);
}

void ChatPanel::submitDraft() {
    auto text = draft();
    if (text.empty()) return;

    if (m_processing) {
        log::debug("panel: stop requested");
        return;
    }

    if (m_onSubmit) {
        m_onSubmit(text);
    }
    clearDraft();
}

void ChatPanel::onSubmit(CCObject* sender) {
    submitDraft();
}

void ChatPanel::onToggleExpanded(CCObject* sender) {
    toggle();
}

void ChatPanel::textInputReturn(CCTextInputNode* node) {
    submitDraft();
}

// ---------------------------------------------------------------------------
// Touch routing (spike S6)
// ---------------------------------------------------------------------------

void ChatPanel::registerWithTouchDispatcher() {
    // swallow = true so that a touch we accept never also reaches the editor;
    // returning false from ccTouchBegan is what lets outside touches through.
    cocos2d::CCDirector::sharedDirector()->getTouchDispatcher()->addTargetedDelegate(
        this, kTouchPriority, true
    );
}

bool ChatPanel::containsPoint(cocos2d::CCPoint const& point) const {
    auto size = getContentSize();
    return point.x >= 0.f && point.x <= size.width
        && point.y >= 0.f && point.y <= size.height;
}

bool ChatPanel::ccTouchBegan(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {
    auto point = convertTouchToNodeSpace(touch);
    if (!containsPoint(point)) {
        return false;
    }
    log::debug("panel: touch began at ({}, {})", point.x, point.y);
    return true;
}

void ChatPanel::ccTouchMoved(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {
    // Claimed so a drag that started inside the panel never lands as an edit.
}

void ChatPanel::ccTouchEnded(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {}

void ChatPanel::ccTouchCancelled(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {}

} // namespace gdai::ui
