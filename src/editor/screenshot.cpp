#include "screenshot.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/loader/Mod.hpp>

#include <algorithm>
#include <filesystem>

#include "../ui/chat_panel.hpp"

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// Hides the interface for the duration of a render, however the capture ends.
///
/// Two things are removed, and for the same reason: a screenshot the model uses to judge
/// the level must show the level.
///
///  - The chat panel, always. It is the assistant's own window and it sits over the view.
///  - The editor's chrome -- toolbars, buttons, tab bars -- unless the caller asks for it.
///    Measured from the first live test: the model could see the UI and reported on it,
///    which is noise it has to filter before it can say anything about the level. Hiding
///    the whole `EditorUI` is exact, unlike cropping to a guessed inset, and the panel is
///    its child so one flag covers both.
///
/// A guard rather than a pair of calls: the render has several early returns, and leaving
/// the interface hidden would be a confusing bug to diagnose from a screenshot that never
/// appears.
class SceneCleaner {
public:
    explicit SceneCleaner(bool hideEditorUi) {
        if (auto* panel = ui::ChatPanel::attached()) {
            m_panel = panel;
            m_panelWasVisible = panel->isVisible();
            panel->setVisible(false);
        }

        if (hideEditorUi) {
            auto* editor = LevelEditorLayer::get();
            if (editor != nullptr && editor->m_editorUI != nullptr) {
                m_ui = editor->m_editorUI;
                m_uiWasVisible = m_ui->isVisible();
                m_ui->setVisible(false);
            }
        }
    }

    ~SceneCleaner() {
        if (m_ui != nullptr) {
            m_ui->setVisible(m_uiWasVisible);
        }
        if (m_panel != nullptr) {
            m_panel->setVisible(m_panelWasVisible);
        }
    }

    SceneCleaner(SceneCleaner const&) = delete;
    SceneCleaner& operator=(SceneCleaner const&) = delete;

private:
    ui::ChatPanel* m_panel = nullptr;
    bool m_panelWasVisible = true;
    cocos2d::CCNode* m_ui = nullptr;
    bool m_uiWasVisible = true;
};

} // namespace

geode::Result<cocos2d::CCRect> selectionBounds() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr || editor->m_editorUI == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }
    auto* selected = editor->m_editorUI->getSelectedObjects();
    if (selected == nullptr || selected->count() == 0) {
        return Err("INVALID_REQUEST: nothing is selected");
    }

    bool any = false;
    float left = 0.f, bottom = 0.f, right = 0.f, top = 0.f;

    auto const total = selected->count();
    for (unsigned int i = 0; i < total; ++i) {
        auto* object = static_cast<GameObject*>(selected->objectAtIndex(i));
        if (object == nullptr) {
            continue;
        }
        // Converted through the object's own parent, so this works at any camera position
        // without touching the camera at all: `getObjectRect` is in the object layer's
        // space and `convertToWorldSpace` takes it the rest of the way.
        auto const rect = object->getObjectRect();
        auto* parent = object->getParent();
        if (parent == nullptr) {
            continue;
        }
        auto const a = parent->convertToWorldSpace({ rect.getMinX(), rect.getMinY() });
        auto const b = parent->convertToWorldSpace({ rect.getMaxX(), rect.getMaxY() });

        auto const objectLeft = std::min(a.x, b.x);
        auto const objectRight = std::max(a.x, b.x);
        auto const objectBottom = std::min(a.y, b.y);
        auto const objectTop = std::max(a.y, b.y);

        if (!any) {
            left = objectLeft;
            right = objectRight;
            bottom = objectBottom;
            top = objectTop;
            any = true;
        } else {
            left = std::min(left, objectLeft);
            right = std::max(right, objectRight);
            bottom = std::min(bottom, objectBottom);
            top = std::max(top, objectTop);
        }
    }

    if (!any) {
        return Err("INVALID_REQUEST: the selection holds no usable objects");
    }
    return Ok(cocos2d::CCRect { left, bottom, right - left, top - bottom });
}

geode::Result<Capture> captureEditor(CaptureRequest const& request) {
    auto* director = cocos2d::CCDirector::sharedDirector();
    auto* scene = director->getRunningScene();
    if (scene == nullptr) {
        return Err("INTERNAL_ERROR: no running scene to capture");
    }

    auto const winSize = director->getWinSize();
    if (winSize.width < 1.f || winSize.height < 1.f) {
        return Err("INTERNAL_ERROR: the window has no size");
    }

    // The region to keep, in point space with the origin at the bottom-left.
    cocos2d::CCRect region { 0.f, 0.f, winSize.width, winSize.height };
    int targets = 0;

    if (request.region == "selection") {
        auto bounds = selectionBounds();
        if (!bounds) {
            return Err(bounds.unwrapErr());
        }
        auto const pad = std::max(0.f, std::min(request.radius, 2000.f));
        auto const raw = bounds.unwrap();
        region = {
            raw.origin.x - pad,
            raw.origin.y - pad,
            raw.size.width + 2.f * pad,
            raw.size.height + 2.f * pad,
        };

        // Clamped to the screen *before* the integer conversion: clamping afterwards would
        // let a negative origin through and produce a texture with a wrapped crop.
        auto const left = std::max(0.f, region.origin.x);
        auto const bottom = std::max(0.f, region.origin.y);
        auto const right = std::min(winSize.width, region.origin.x + region.size.width);
        auto const top = std::min(winSize.height, region.origin.y + region.size.height);
        if (right - left < 8.f || top - bottom < 8.f) {
            return Err(
                "INVALID_REQUEST: the selection is off screen -- scroll it into view first"
            );
        }
        region = { left, bottom, right - left, top - bottom };

        if (auto* editor = LevelEditorLayer::get()) {
            if (auto* ui = editor->m_editorUI) {
                if (auto* selected = ui->getSelectedObjects()) {
                    targets = static_cast<int>(selected->count());
                }
            }
        }
    }

    // Whole-screen captures are kept at exactly the window size; a crop only gets smaller.
    // Rendering larger would mean scaling the visit, which means moving or scaling the live
    // scene -- a one-frame lie about its state with no way to recover from a crash inside.
    auto const width = std::max(8, static_cast<int>(region.size.width));
    auto const height = std::max(8, static_cast<int>(region.size.height));

    // The interface goes away for the render; `includeUi` puts it back when a caller
    // genuinely wants to see the editor's own state rather than the level.
    // The constructor takes "hide the UI", the request says "include it": passing the
    // request's flag unnegated hid the UI only when it was asked for (2026-11-14: the
    // model said its screenshots were mostly toolbars).
    SceneCleaner clean(!request.includeUi);
    // 1. The whole view, at window size.
    auto* full = cocos2d::CCRenderTexture::create(
        static_cast<int>(winSize.width), static_cast<int>(winSize.height)
    );
    if (full == nullptr) {
        return Err("INTERNAL_ERROR: could not create the render texture");
    }
    full->begin();
    scene->visit();
    full->end();

    // 2. The region, cut out of it. Draw the full texture through a sprite whose texture
    //    rect is the region, into a texture of the region's size. Cheaper and safer than
    //    moving the scene, and it needs no pixel access at all.
    cocos2d::CCRenderTexture* cropped = full;
    cocos2d::CCSprite* sprite = nullptr;
    if (width != static_cast<int>(winSize.width) || height != static_cast<int>(winSize.height)) {
        sprite = cocos2d::CCSprite::createWithTexture(full->getSprite()->getTexture());
        if (sprite == nullptr) {
            return Err("INTERNAL_ERROR: could not prepare the crop");
        }
        sprite->setTextureRect(region);
        sprite->setAnchorPoint({ 0.f, 0.f });
        sprite->setPosition({ 0.f, 0.f });

        cropped = cocos2d::CCRenderTexture::create(width, height);
        if (cropped == nullptr) {
            return Err("INTERNAL_ERROR: could not create the cropped texture");
        }
        cropped->begin();
        sprite->visit();
        cropped->end();
    }

    // 3. Write it.
    auto* image = cropped->newCCImage();
    if (image == nullptr) {
        return Err("INTERNAL_ERROR: could not read the rendered texture");
    }

    // `getSaveDir` returns `asp::fs::Path`, not a `std::filesystem::path`, so it is
    // converted through `string()`. Using `std` here rather than `asp` because
    // `CCImage::saveToFile` wants a plain C string anyway.
    auto const directory =
        std::filesystem::path(Mod::get()->getSaveDir().string()) / "captures";
    std::error_code error;
    // Created unconditionally rather than checked first: `create_directories` already
    // succeeds when it exists, and the `error_code` overload stops an exception escaping
    // into a rendering path.
    std::filesystem::create_directories(directory, error);
    if (error) {
        log::warn("capture: could not create {}: {}", directory.string(), error.message());
    }

    // Fixed name, overwritten each time: captures never accumulate, and nothing has to
    // clean up after a sidecar that crashes between reading and deleting.
    auto const path = (directory / "capture.png").string();
    auto const saved = image->saveToFile(path.c_str());
    delete image;

    if (!saved) {
        return Err(fmt::format("INTERNAL_ERROR: could not write the capture to {}", path));
    }

    Capture capture;
    capture.path = path;
    capture.width = width;
    capture.height = height;
    capture.x = region.origin.x;
    capture.y = region.origin.y;
    capture.regionWidth = region.size.width;
    capture.regionHeight = region.size.height;
    capture.viewport = screenRectToLevel(region);
    capture.targets = targets;

    log::info(
        "capture: {}x{} at ({}, {}) from {} target(s) -> {}",
        width, height, region.origin.x, region.origin.y, targets, path
    );
    return Ok(std::move(capture));
}

matjson::Value captureToJson(Capture const& capture) {
    auto result = matjson::Value::object();
    result["path"] = capture.path;
    result["width"] = capture.width;
    result["height"] = capture.height;
    result["targets"] = capture.targets;

    auto region = matjson::Value::object();
    region["x"] = static_cast<double>(capture.x);
    region["y"] = static_cast<double>(capture.y);
    region["width"] = static_cast<double>(capture.regionWidth);
    region["height"] = static_cast<double>(capture.regionHeight);
    result["region"] = std::move(region);

    // What the image covers in level coordinates. The screen-space `region` above is the
    // crop, which is what the renderer needed; this is what a caller comparing the picture
    // against object positions needs, and they are not the same numbers.
    auto viewport = matjson::Value::object();
    viewport["x1"] = capture.viewport.x1;
    viewport["y1"] = capture.viewport.y1;
    viewport["x2"] = capture.viewport.x2;
    viewport["y2"] = capture.viewport.y2;
    result["viewport"] = std::move(viewport);
    return result;
}

} // namespace gdai::editor
