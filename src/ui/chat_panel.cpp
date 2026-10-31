#include "chat_panel.hpp"

#include "panel_host.hpp"

#include <Geode/binding/EditorUI.hpp>
#include <Geode/modify/CCMouseDispatcher.hpp>
#include <Geode/modify/CCTextInputNode.hpp>
#include <Geode/modify/EditorUI.hpp>
#include <Geode/ui/Button.hpp>
#include <Geode/ui/Label.hpp>
#include <Geode/ui/NineSlice.hpp>
#include <Geode/ui/ScrollLayer.hpp>
#include <Geode/ui/TextInput.hpp>
#include <Geode/utils/cocos.hpp>

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <limits>

using namespace geode::prelude;

namespace gdai::ui {

ChatPanel* ChatPanel::s_attached = nullptr;

namespace {

/// Wall-clock-independent milliseconds, for the playtest suppression window.
double monotonicMs() {
    using namespace std::chrono;
    return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count();
}

/// While this is in the future, Enter must not start playtest.
///
/// Set by `submitDraft`, read by the `EditorUI::onPlaytest` hook. Blocking the
/// *action* rather than the key is what makes this work: the shortcut does not go
/// through `EditorUI::keyDown` in v6, so swallowing the key left it starting anyway.
double g_suppressPlaytestUntilMs = 0.0;

/// How long to keep blocking playtest after a submit.
///
/// Long enough to cover key-down, the editor's key handling and the resulting
/// action; short enough that pressing Enter again deliberately still plays the
/// level. 750 ms is far longer than a frame and far shorter than a person.
constexpr double kPlaytestSuppressMs = 750.0;


constexpr float kPad = 6.f;
constexpr float kHeaderHeight = 22.f;
constexpr float kInputHeight = 26.f;
constexpr float kButtonWidth = 46.f;

/// Vertical space between messages, and the inset inside the scroll area.
///
/// Named because `refreshLayout` places children by hand: the gap and the padding are
/// now part of the layout arithmetic rather than a layout object's business.
constexpr float kMessageGap = 4.f;
constexpr float kMessagePad = 2.f;

/// Touch priority for the panel's claim on the message area.
///
/// **Behind** the panel's own children, which is the point. Lower values are
/// dispatched first, so every value that was tried below the children (`-400`,
/// `-499`) made the panel win the touch and swallow the drag before the scroll
/// layer saw it, which is why the transcript stopped scrolling. `-1` sits behind
/// GD's menus (`-128`) and behind the scroll layer, and still ahead of the editor
/// layer at `0`, which is the combination that lets the children work and keeps the
/// editor from panning underneath the panel.
constexpr int kTouchPriority = -1;

/// Size of the status dot, in points.
constexpr float kDotSize = 12.f;

/// Scale applied to message text.
///
/// Only meaningful now that the layout no longer scales children: while it did, it
/// overrode this value, so the text stayed at whatever size the layout chose.
constexpr float kTextScale = 0.42f;

/// Scale for the input field, the send button and the title.
///
/// Separate from `kTextScale` only because a text field carries its own internal
/// padding; keeping them equal would make the input noticeably chunkier than the
/// transcript above it.
constexpr float kInputScale = 0.6f;

/// The bitmap font used for panel text.
///
/// `chatFont.fnt`, not `bigFont.fnt`, and the difference is not cosmetic. GD's two
/// general-purpose fonts differ in ways that are easy to miss from the names:
///
///  - `bigFont.fnt` is **size 32** and its lowercase glyphs are drawn as capitals,
///    so "Finished with 2 actions" renders as "FINISHED WITH 2 ACTIONS" at twice
///    the intended height. That combination is why a screenshot showed an all-caps
///    wall of text no font-scale change appeared to shrink.
///  - `chatFont.fnt` is **size 16**, has real lowercase, and is the font GD itself
///    uses for chat -- which is exactly what this panel is.
///
/// Both fonts contain the punctuation that matters (`!`, `'`), which was worth
/// checking directly: the earlier suspicion that a font was rejecting `!` was
/// wrong, and the real cause was the input's character filter.
constexpr char const* kFont = "chatFont.fnt";

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

/// True for the roles whose text is trusted, because this mod wrote it.
///
/// SPEC.md 8.3.1: assistant text and anything echoed from level data is
/// untrusted. System messages are ours, so they may use colour tags.
bool roleIsTrusted(std::string_view role) {
    return role == "system";
}

/// Strips the markup that must never reach the renderer (SPEC.md 8.3.1).
///
/// Angle-bracket tags are removed wholesale rather than allow-listed. The
/// distinction matters for colour tags specifically: `geode::Label::create`
/// renders plain text, so a stray `<cr>` would show up literally rather than
/// colouring anything -- but the renderer is a detail of this file, and a
/// future switch to the rich-text path (which `MDTextArea` uses) would silently
/// turn every unstripped tag into a formatting instruction. Stripping here
/// means that switch cannot become a security change by accident.
///
/// An unterminated `<` is kept, since dropping the rest of the message because
/// a closing bracket never arrived would lose far more than it protects.
std::string sanitise(std::string const& text) {
    std::string out;
    out.reserve(text.size());

    for (size_t i = 0; i < text.size(); ++i) {
        if (text[i] != '<') {
            out.push_back(text[i]);
            continue;
        }
        auto const close = text.find('>', i + 1);
        if (close == std::string::npos) {
            out.append(text, i, std::string::npos);
            break;
        }
        i = close;
    }
    return out;
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

    // The panel claims the message area only -- see the class comment. Children
    // (scroll layer, buttons, input) handle their own regions.
    setTouchEnabled(true);


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

    m_statusDot = cocos2d::CCLayerColor::create({ 170, 170, 180, 255 }, kDotSize, kDotSize);
    if (!m_statusDot) {
        log::warn("panel: status dot could not be created");
    } else {
        m_header->addChild(m_statusDot, 2);
    }

    auto title = Label::create("LLM Assistant", kFont);
    if (title) {
        title->setAnchorPoint({ 0.f, 0.5f });
        title->setScale(kInputScale);
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
    auto height = messageAreaHeight();

    m_messages = ScrollLayer::create({ width, height }, true, true);
    if (!m_messages) {
        log::error("panel: ScrollLayer::create returned null");
        return;
    }
    m_messages->setAnchorPoint({ 0.f, 0.f });
    m_messages->setID("messages"_spr);
    // Drag-scrolling works through the touch dispatch, but the mouse wheel does
    // not: GD routes wheel events only to layers that have opted into mouse
    // handling, so without this the wheel is delivered to the editor behind the
    // panel and pans the level instead.
    m_messages->setMouseEnabled(true);

    if (auto content = m_messages->m_contentLayer) {
        // Laid out manually rather than with a `Layout`.
        //
        // Two reasons, both of which were bugs rather than preferences:
        //
        //  - A layout sizes the content layer from children's **unscaled** sizes while
        //    the labels are scaled down, so the layer and its children disagreed. The
        //    logged symptom was `content=204x124 view=204x124` with children positioned
        //    at y=179 -- the content layer never grew, so there was no scroll range and
        //    neither dragging nor the wheel could move anything.
        //  - `setAutoGrowAxis` was lost when this moved from `ColumnLayout` to
        //    `SimpleAxisLayout`, which has no such method. The comment claiming it was
        //    there survived, which is how it went unnoticed: the code read correctly.
        //
        // Doing the arithmetic here means the layer height and the child positions come
        // from the same numbers, so they cannot drift apart again.
        m_messageContent = content;
    } else {
        log::error("panel: ScrollLayer has no content layer");
    }

    addChild(m_messages, 1);

    m_emptyHint = Label::create("No messages yet.", kFont);
    if (m_emptyHint) {
        // Scaled like every other label. It was previously left at scale 1, which
        // made the placeholder the largest text in the panel -- and it is the first
        // thing on screen, so it dominated the impression of the text size.
        m_emptyHint->setScale(kTextScale);
        m_emptyHint->setColor({ 150, 150, 160 });
        // Added to the scroll layer rather than the content layer, and centred
        // explicitly. At the default (0,0) inside a bottom-anchored list it sits
        // half off the bottom-left corner, overlapping the input row -- which
        // reads as a rendering bug in an unrelated part of the panel.
        m_emptyHint->setPosition({ width * 0.5f, height * 0.5f });
        m_messages->addChild(m_emptyHint, 2);
    }
}

void ChatPanel::buildInput() {
    m_inputRow = cocos2d::CCNode::create();
    addChild(m_inputRow, 1);

    // `chatFont.fnt` rather than `bigFont.fnt`, which is also the default this
    // overload documents. GD's text field restricts typed characters to the
    // glyphs the font provides, so a font missing `!` makes that key appear dead
    // -- and `chatFont.fnt`, whatever it contains, was rejecting it.
    auto input = TextInput::create(100.f, "Ask about this level...", "bigFont.fnt");
    if (input) {
        input->setScale(kInputScale);
        input->setMaxCharCount(2000); // matches the mod's max-message-length default
        // Explicitly "anything typeable". Leaving the filter unset has GD apply a
        // default that rejects punctuation such as `!` and `'`, which matters here:
        // an apostrophe is ordinary English, and a chat box that cannot type one is
        // not usable.
        input->setCommonFilter(CommonFilter::Any);
        input->setID("input"_spr);
        m_inputRow->addChild(input, 1);
        m_input = input;

        // Take over the node's delegate so Enter can be seen, chaining to the
        // delegate `TextInput` had installed there so its own behaviour is
        // unchanged. See `InputForwarder`.
        if (auto node = input->getInputNode()) {
            m_inputNode = node;
            s_attached = this;
            m_inputForwarder.setInner(node->m_delegate);
            m_inputForwarder.setOnEnter([this] {
                log::info("panel: Enter via enterPressed");
                submitDraft();
            });
            node->setDelegate(&m_inputForwarder);
            log::info("panel: installed the Enter forwarder on the input node");
        } else {
            log::warn("panel: input node missing; Enter will not submit");
        }
    }

    m_buttonLabel = cocos2d::CCLabelBMFont::create("Send", kFont);
    if (m_buttonLabel) {
        m_buttonLabel->setScale(kInputScale);
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

float ChatPanel::messageAreaHeight() const {
    return m_size.height - kHeaderHeight - kInputHeight - 4.f * kPad;
}

cocos2d::CCRect ChatPanel::messageAreaRect() const {
    return {
        kPad,
        kPad + kInputHeight + kPad,
        m_size.width - 2.f * kPad,
        messageAreaHeight(),
    };
}

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
        // Clear of the title rather than underneath it: at the original spacing the
        // dot and the first letters of the title occupied the same space, so a
        // working dot was easy to read as a missing one. Anchored bottom-left, so
        // the y is the bottom of the dot rather than its centre.
        m_statusDot->setPosition({ kPad + 4.f, (kHeaderHeight - kDotSize) * 0.5f });
    }
    if (auto title = m_header ? m_header->getChildByID("title"_spr) : nullptr) {
        title->setPosition({ kPad + 26.f, kHeaderHeight * 0.5f });
    }
    if (auto collapse = m_header ? m_header->getChildByID("collapse"_spr) : nullptr) {
        collapse->setPosition({ width - kPad - 7.f, kHeaderHeight * 0.5f });
    }

    auto area = messageAreaRect();
    if (m_messages) {
        m_messages->setVisible(m_expanded);
        m_messages->setPosition({ area.origin.x, area.origin.y });
        m_messages->setContentSize({ area.size.width, area.size.height });
    }
    if (m_emptyHint) {
        m_emptyHint->setPosition({ area.size.width * 0.5f, area.size.height * 0.5f });
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

    // Re-laid out rather than just re-scrolled: collapsing and expanding changes the
    // visible height, and the wrapping of every message depends on it.
    refreshLayout();
}

void ChatPanel::toggle() {
    m_expanded = !m_expanded;
    relayout();
    log::debug("panel: expanded = {}", m_expanded);
}

ChatPanel* ChatPanel::attached() {
    return s_attached;
}

void ChatPanel::applyVisibility() {
    // Two independent reasons to be invisible, so visibility is derived rather than
    // set directly: the user hid it, or the player is testing the level. Deriving it
    // means neither can undo the other -- leaving playtest must not reveal a panel
    // the user chose to hide.
    setVisible(!m_hidden && !m_playtestHidden);
}

void ChatPanel::setHidden(bool hidden) {
    if (m_hidden == hidden) {
        return;
    }
    m_hidden = hidden;
    applyVisibility();
    log::info("panel: {} by the user", hidden ? "hidden" : "shown");
}

void ChatPanel::setPlaytestHidden(bool hidden) {
    if (m_playtestHidden == hidden) {
        return;
    }
    m_playtestHidden = hidden;

    // Drop keyboard focus on the way into playtest.
    //
    // While the text field is active it takes every keystroke, and the game does not
    // take it away when the level starts: an Enter pressed in an empty box starts a
    // playtest (that is the editor's own shortcut) and leaves focus in the field, so
    // the player's movement keys do nothing and whatever they type during the attempt
    // appears in the panel afterwards. Releasing it here fixes both, and it is done
    // from the playtest transition rather than from the key handler so it applies
    // however the playtest started.
    if (hidden && m_input) {
        m_input->defocus();
    }

    applyVisibility();
    log::info("panel: {} for playtest", hidden ? "hidden" : "shown");
}

bool ChatPanel::handleScroll(float y) {
    if (m_hidden || m_playtestHidden || !m_expanded || m_messages == nullptr) {
        return false;
    }

    // Only claim the wheel over the transcript. Everywhere else on the panel -- the
    // header, the input row, the margins -- the event belongs to whatever is behind,
    // and swallowing it there would make the panel a dead zone for the editor.
    auto const mouse = convertToNodeSpace(geode::cocos::getMousePos());
    if (!messageAreaRect().containsPoint(mouse)) {
        return false;
    }

    // The scroll layer already knows how to scroll itself; this only has to deliver
    // the event and claim it. Doing the arithmetic here instead would duplicate logic
    // that has to agree with the drag path.
    m_messages->scrollWheel(y, 0.f);
    return true;
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
    upsertMessage({}, role, text);
}

std::string stripFences(std::string const& text) {
    std::string out;
    out.reserve(text.size());

    std::size_t start = 0;
    while (start <= text.size()) {
        auto const end = text.find('\n', start);
        auto const length = end == std::string::npos ? std::string::npos : end - start;
        auto const line = text.substr(start, length);

        if (!line.starts_with("```")) {
            out.append(line);
            out.push_back('\n');
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1;
    }

    // A trailing newline is harmless in itself, but it makes the label taller
    // than the text needs, which shows up as an unexplained gap under the last
    // message.
    while (!out.empty() && out.back() == '\n') {
        out.pop_back();
    }
    return out;
}

float ChatPanel::usableTextWidth() const {
    // Inset a little beyond the padding so the scrollbar and the rounded
    // background edge do not clip the last character of a line.
    return messageAreaRect().size.width - 10.f;
}

geode::Label* ChatPanel::makeMessageLabel(std::string_view role, std::string const& text) {
    auto shown = roleIsTrusted(role) ? text : stripFences(sanitise(text));

    auto label = Label::create(shown, kFont);
    if (!label) return nullptr;
    label->setAnchorPoint({ 0.f, 1.f });
    label->setColor(roleColor(role));
    label->setAlignment(Label::Alignment::Left);

    // Wrap at the visible width expressed in unscaled units, then scale down.
    //
    // The division is the point: `setMaxWidth` is compared against the label's
    // *unscaled* width, so wrapping at the plain visible width would leave the
    // text at 1/scale of the panel once scaled. Dividing by the scale makes the
    // displayed line length come out at exactly the width we want.
    label->setMaxWidth(usableTextWidth() / kTextScale);
    label->setScale(kTextScale);
    return label;
}

void ChatPanel::upsertMessage(
    std::string const& id, std::string_view role, std::string const& text
) {
    if (!m_messageContent) return;

    if (!id.empty()) {
        if (auto it = m_labelById.find(id); it != m_labelById.end()) {
            m_textById[id] = text;
            if (it->second) {
                it->second->setText(text);
            }
            refreshLayout();
            return;
        }
    }

    auto label = makeMessageLabel(role, text);
    if (!label) return;

    if (!id.empty()) {
        m_labelById[id] = label;
        m_textById[id] = text;
        m_idOrder.push_back(id);
    }

    appendMessageNode(label);
}

void ChatPanel::appendMessageDelta(std::string const& id, std::string const& delta) {
    if (id.empty() || delta.empty()) return;

    // Deltas are accumulated against the panel's own copy rather than the
    // label's, because a delta may arrive after an `assistant_message` that
    // already set the full text, and re-reading the label would then be reading
    // back our own write rather than the authoritative text.
    auto& text = m_textById[id];
    if (text.empty()) {
        // A delta with no prior text is a new message; the role is assistant by
        // definition, since only assistant text is streamed.
        upsertMessage(id, "assistant", delta);
        return;
    }
    text += delta;
    if (auto it = m_labelById.find(id); it != m_labelById.end() && it->second) {
        it->second->setText(text);
    }
    refreshLayout();
}

void ChatPanel::clearMessages() {
    if (m_thinkingNode) {
        unschedule(schedule_selector(ChatPanel::tickThinking));
    }
    for (auto node : m_messageNodes) {
        if (node && node->getParent()) {
            node->removeFromParent();
        }
    }
    m_messageNodes.clear();
    m_labelById.clear();
    m_textById.clear();
    m_idOrder.clear();
    m_thinkingNode = nullptr;
    m_thinkingStep = 0;

    if (m_emptyHint) {
        m_emptyHint->setVisible(true);
    }
    if (m_messageContent) {
        // A layout pass, not `updateLayout`: there is no `Layout` on the content layer
        // any more, so the height has to be recomputed here or the layer keeps the size
        // the last message gave it.
        refreshLayout();
    }
}

void ChatPanel::setThinking(bool on) {
    if (!m_messageContent) return;

    if (!on) {
        if (m_thinkingNode) {
            unschedule(schedule_selector(ChatPanel::tickThinking));
            m_messageNodes.erase(
                std::remove(m_messageNodes.begin(), m_messageNodes.end(), m_thinkingNode),
                m_messageNodes.end()
            );
            if (m_thinkingNode->getParent()) {
                m_thinkingNode->removeFromParent();
            }
            m_thinkingNode = nullptr;
            refreshLayout();
        }
        return;
    }

    if (m_thinkingNode) return;

    auto label = cocos2d::CCLabelBMFont::create("Thinking", kFont);
    if (!label) return;
    label->setAnchorPoint({ 0.f, 1.f });
    label->setScale(kTextScale);
    label->setColor(roleColor("thinking"));
    m_thinkingStep = 0;

    appendMessageNode(label);
    m_thinkingNode = label;
    unschedule(schedule_selector(ChatPanel::tickThinking));
    schedule(schedule_selector(ChatPanel::tickThinking), 0.4f);
}

void ChatPanel::tickThinking(float dt) {
    if (!m_thinkingNode) {
        unschedule(schedule_selector(ChatPanel::tickThinking));
        return;
    }
    // `.` -> `..` -> `...` and back (SPEC.md 8.9). Plain ASCII, because a font
    // without a glyph for a fancier character degrades to a blank box.
    static constexpr std::array<char const*, 3> kDots { ".", "..", "..." };
    m_thinkingStep = (m_thinkingStep + 1) % static_cast<int>(kDots.size());
    m_thinkingNode->setString(
        fmt::format("Thinking{}", kDots[static_cast<size_t>(m_thinkingStep)]).c_str()
    );
}

void ChatPanel::showTurnSummary(
    std::string const& status, std::vector<std::string> const& lines
) {
    setThinking(false);

    std::string text;
    if (status == "completed") {
        text = fmt::format("Finished with {} action{}", lines.size(), lines.size() == 1 ? "" : "s");
    } else if (status == "cancelled") {
        text = "Stopped";
    } else if (status == "incomplete") {
        text = "Turn did not finish (editor or connection closed)";
    } else {
        text = fmt::format("Turn failed ({})", status);
    }
    for (auto const& line : lines) {
        text += fmt::format("\n  - {}", line);
    }

    upsertMessage({}, status == "failed" ? "error" : "action-summary", text);
}

void ChatPanel::refreshLayout() {
    if (!m_messageContent || !m_messages) return;

    auto const viewWidth = m_messages->getContentSize().width;
    auto const viewHeight = m_messages->getContentSize().height;
    auto const gap = kMessageGap;

    // Measure with **scaled** sizes, because that is what is drawn. Using the
    // unscaled size here was part of why the content layer and its children
    // disagreed about how tall the transcript was.
    //
    // `isVisible` is deliberately *not* consulted. The content layer culls children
    // that scroll out of view -- it sets their `visible` flag from inside
    // `setPosition` -- so skipping the invisible ones here would measure a transcript
    // that shrinks as soon as it is scrolled, and the next layout would then pack the
    // remaining messages on top of the culled ones. That is what overlapping text
    // looks like: two independent notions of where a message belongs.
    float total = 0.f;
    for (auto* node : m_messageNodes) {
        if (node == nullptr) {
            continue;
        }
        total += node->getScaledContentSize().height + gap;
    }
    if (total > 0.f) {
        total -= gap;  // no gap after the last item
    }

    // At least as tall as the viewport, so a short conversation sits against the
    // bottom instead of floating in the middle -- and *more* than the viewport once
    // it overflows, which is what gives the scroll layer something to scroll.
    auto const contentHeight = std::max(viewHeight, total + 2.f * kMessagePad);
    m_messageContent->setContentSize({ viewWidth, contentHeight });

    // Newest at the bottom, built upwards from the padding.
    //
    // Bottom-up rather than top-down because the bottom of the content is the end the
    // scroll layer treats as a resting position (`contentY == 0`), so the newest
    // message lands exactly where `scrollToBottom` goes looking for it. The labels
    // anchor top-left, so the y stored here is each message's top edge.
    float cursor = kMessagePad;
    for (auto it = m_messageNodes.rbegin(); it != m_messageNodes.rend(); ++it) {
        auto* node = *it;
        if (node == nullptr) {
            continue;
        }
        cursor += node->getScaledContentSize().height;
        node->setPosition({ kMessagePad, cursor });
        cursor += gap;
    }

    scrollToBottom();
}

void ChatPanel::scrollToBottom() {
    if (!m_messageContent || !m_messages) return;

    // "Jump to the newest message" is not a scroll gesture, so it is done through the
    // scroll layer's own arithmetic instead of by writing a position.
    //
    // Writing the position directly is what broke this: the content layer is a
    // `GenericContentLayer`, and its `setPosition` **overrides** the y it is handed
    // whenever the content is shorter than the view. Asking for `+overflow` therefore
    // did not put the content one screen-height down; it put it *above* the viewport,
    // where the layer's own culling marked every message invisible. The panel went
    // blank while still believing it had scrolled correctly.
    //
    // `scrollToFromTop` clamps the offset itself, so a huge offset simply means "the
    // far end", i.e. the newest message. When the content fits, the clamp resolves to
    // zero overflow and this settles at rest.
    m_messages->scrollToFromTop(std::numeric_limits<float>::max());
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

        if (!m_idOrder.empty()) {
            auto const evicted = m_idOrder.front();
            m_idOrder.erase(m_idOrder.begin());
            m_labelById.erase(evicted);
            m_textById.erase(evicted);
        }

        if (oldest && oldest->getParent()) {
            oldest->removeFromParent();
        }
    }

    refreshLayout();
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

void ChatPanel::setOnCancel(geode::Function<void()> onCancel) {
    m_onCancel = std::move(onCancel);
}

void ChatPanel::submitDraft() {
    // While a turn is running the button is Stop (SPEC.md 8.5), so pressing it
    // or Enter asks the sidecar to abort rather than starting a second turn.
    if (m_processing) {
        log::info("panel: stop requested");
        if (m_onCancel) {
            m_onCancel();
        }
        return;
    }

    auto text = draft();
    log::info(
        "panel: submit attempted, chars={} empty={} hasHandler={}",
        text.size(), text.empty(), static_cast<bool>(m_onSubmit)
    );
    if (text.empty()) return;

    if (m_onSubmit) {
        m_onSubmit(text);
    }
    clearDraft();

    // Arm the playtest block here rather than in a key hook, so it applies whatever
    // path submitted: the input node's insertion callback, `EditorUI::keyDown`, or
    // the Send button. Only the key paths need it, but doing it unconditionally costs
    // nothing and cannot be bypassed.
    g_suppressPlaytestUntilMs = monotonicMs() + kPlaytestSuppressMs;
}

void ChatPanel::onSubmit(CCObject* sender) {
    log::info("panel: send button pressed");
    submitDraft();
}

void ChatPanel::onToggleExpanded(CCObject* sender) {
    log::info("panel: collapse button pressed");
    toggle();
}

void ChatPanel::textInputReturn(CCTextInputNode* node) {
    log::info("panel: enter pressed in the input");
    submitDraft();
}

// ---------------------------------------------------------------------------
// Input forwarding (SPEC.md 8.5)
// ---------------------------------------------------------------------------

void InputForwarder::textChanged(CCTextInputNode* node) {
    if (m_inner) m_inner->textChanged(node);
}

void InputForwarder::textInputOpened(CCTextInputNode* node) {
    if (m_inner) m_inner->textInputOpened(node);
}

void InputForwarder::textInputClosed(CCTextInputNode* node) {
    if (m_inner) m_inner->textInputClosed(node);
}

void InputForwarder::textInputShouldOffset(CCTextInputNode* node, float yOffset) {
    if (m_inner) m_inner->textInputShouldOffset(node, yOffset);
}

bool InputForwarder::allowTextInput(CCTextInputNode* node) {
    // Defaults to allowing when there is nothing to chain to, mirroring what the
    // base class does. Refusing would make the field silently uneditable.
    return m_inner ? m_inner->allowTextInput(node) : true;
}

void InputForwarder::enterPressed(CCTextInputNode* node) {
    log::info("panel: Enter via enterPressed");
    if (m_onEnter) m_onEnter();
    if (m_inner) m_inner->enterPressed(node);
}

void InputForwarder::textInputReturn(CCTextInputNode* node) {
    log::info("panel: Enter via textInputReturn");
    if (m_onEnter) m_onEnter();
    if (m_inner) m_inner->textInputReturn(node);
}

void ChatPanel::onExit() {
    unschedule(schedule_selector(ChatPanel::tickThinking));
    if (s_attached == this) {
        s_attached = nullptr;
    }
    m_inputNode = nullptr;
    CCLayer::onExit();
}

bool ChatPanel::ownsInputNode(CCTextInputNode* node) {
    return node != nullptr && s_attached != nullptr && s_attached->m_inputNode == node;
}

void ChatPanel::submitFromInputNode() {
    if (s_attached) {
        s_attached->submitDraft();
    }
}

bool ChatPanel::inputHasFocus() {
    if (s_attached == nullptr || s_attached->m_inputNode == nullptr) {
        return false;
    }
    // `m_selected` is the node's own focus flag, which avoids having to track focus
    // separately through callbacks that are never delivered.
    return s_attached->m_inputNode->m_selected;
}

// ---------------------------------------------------------------------------
// Touch (SPEC.md 8.1)
// ---------------------------------------------------------------------------

void ChatPanel::registerWithTouchDispatcher() {
    cocos2d::CCDirector::sharedDirector()->getTouchDispatcher()->addTargetedDelegate(
        this, kTouchPriority, true
    );
}

bool ChatPanel::ccTouchBegan(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {
    auto point = convertTouchToNodeSpace(touch);
    // Claim the message area so a scroll drag cannot also pane the editor. The
    // header (collapse button) and the input row (text input, send) are left
    // alone: claiming them is what previously made Send look broken.
    auto claimed = m_expanded && messageAreaRect().containsPoint(point);
    log::debug("panel: touch at ({}, {}) claimed={}", point.x, point.y, claimed);
    return claimed;
}

void ChatPanel::ccTouchMoved(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {}

void ChatPanel::ccTouchEnded(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {}

void ChatPanel::ccTouchCancelled(cocos2d::CCTouch* touch, cocos2d::CCEvent* event) {}

// ---------------------------------------------------------------------------
// Input hook (SPEC.md 8.5)
// ---------------------------------------------------------------------------

// The only place GD surfaces the key that produced typed input. See the class
// comment for the two mechanisms that were measured not to work.
//
// Chained rather than replaced: the original is what inserts the character, so
// skipping it would make the field read-only.
class $modify(ChatInputNodeHook, CCTextInputNode) {
    bool onTextFieldInsertText(
        cocos2d::CCTextFieldTTF* sender, char const* text, int nLen,
        cocos2d::enumKeyCodes keyCodes
    ) {
        if (gdai::ui::ChatPanel::ownsInputNode(this)) {
            // Logged unconditionally: the key code is the answer to a question that
            // has cost several rounds, and it is cheap to record while the answer
            // is still uncertain.
            log::info(
                "panel: insert text len={} keyCode=0x{:X} text='{}'",
                nLen,
                static_cast<int>(keyCodes),
                text ? std::string(text, nLen) : std::string()
            );
            if (keyCodes == cocos2d::KEY_Enter) {
                log::info("panel: Enter via the input node hook");
                gdai::ui::ChatPanel::submitFromInputNode();
            }
        }
        return CCTextInputNode::onTextFieldInsertText(sender, text, nLen, keyCodes);
    }
};

// Enter arrives here, not at the text field.
//
// The text-insertion hook above sees every typed character and reports
// `KEY_Unknown` for all of them, and never sees Enter at all -- so GD consumes
// Enter somewhere before the field's own callback. What it does do is dispatch key
// events to `EditorUI::keyDown`, which is measurable: Geode's keybind mod installs
// a hook on exactly this function and it fires. Going through the same door is
// therefore a supported route rather than a guess.
//
// Chained, so the editor's own key handling is untouched.
//
// Enter needs more than swallowing the key event. Swallowing `keyDown` and then
// `keyUp` both proved insufficient: sending a message still started playtest,
// because the shortcut is not resolved through `EditorUI::keyDown` at all (v6 added
// `CCKeyboardActionManager`, and plenty of editor shortcuts go through keybind
// handling rather than the layer's own virtual).
//
// So the *action* is blocked instead: `onPlaytest` is swallowed for the window
// `submitDraft` arms. That is trigger-path independent, which is what makes it work.
class $modify(ChatEditorKeyHook, EditorUI) {
    void keyDown(cocos2d::enumKeyCodes key, double timestamp) {
        if (key == cocos2d::KEY_Enter && gdai::ui::ChatPanel::inputHasFocus()) {
            log::info("panel: Enter via EditorUI::keyDown");
            gdai::ui::ChatPanel::submitFromInputNode();
            return;
        }
        EditorUI::keyDown(key, timestamp);
    }

    void onPlaytest(cocos2d::CCObject* sender) {
        if (monotonicMs() < g_suppressPlaytestUntilMs) {
            // Logged rather than silent: this is the only place that proves which
            // path the shortcut takes, and that has now been guessed wrong twice.
            log::info("panel: blocking the playtest the message-sending Enter started");
            return;
        }
        // Hidden here, not only by the per-frame playtest poll.
        //
        // This is the action that starts playtest whatever triggered it -- button,
        // keybind, or menu -- so it fires earlier and more reliably than comparing
        // `PlayLayer::get()` against its previous value. Hiding before the original
        // runs means the panel is gone before the first frame of the level is drawn.
        gdai::ui::PanelHost::get().setPlaytestHidden(true);
        EditorUI::onPlaytest(sender);
    }

    // The two ways back out of playtest. Both are hooked because either can be the one
    // that runs, depending on how the player leaves; setting the flag twice is a no-op,
    // so covering both costs nothing and means a missed path cannot strand the panel
    // hidden.
    void onStopPlaytest(cocos2d::CCObject* sender) {
        gdai::ui::PanelHost::get().setPlaytestHidden(false);
        EditorUI::onStopPlaytest(sender);
    }

    void playtestStopped() {
        gdai::ui::PanelHost::get().setPlaytestHidden(false);
        EditorUI::playtestStopped();
    }
};

// Mouse-wheel scrolling over the transcript.
//
// The dispatcher has no priority argument -- `addDelegate`, `forceAddDelegate` and no
// ordering controls -- so registering the panel as another wheel handler would not make
// it beat the editor, which is already registered. The editor kept the wheel and panned
// the level instead.
//
// Hooking `dispatchScrollMSG` sidesteps the ordering problem entirely: the event is
// checked once, at the point where it is dispatched, and either consumed here or passed
// on untouched. That only became possible in Geode v6, where `scrollWheel` returns
// `bool` -- before that there was no way to say "I handled this".
class $modify(ChatScrollHook, cocos2d::CCMouseDispatcher) {
    bool dispatchScrollMSG(float y, float x) {
        if (auto* panel = gdai::ui::ChatPanel::attached()) {
            if (panel->handleScroll(y)) {
                // Returning true consumes the event. Chaining as well would scroll the
                // transcript *and* the level, which is the bug this hook exists to fix.
                return true;
            }
        }
        return cocos2d::CCMouseDispatcher::dispatchScrollMSG(y, x);
    }
};

} // namespace gdai::ui
