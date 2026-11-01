#include "camera.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>

#include <algorithm>
#include <cmath>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// The zoom range the editor's own controls allow, clamped rather than refused.
///
/// Zoom is a *view* setting, not a level property: an out-of-range request has an obvious
/// correct interpretation ("as far as it goes") and no chance of damaging anything, which
/// is the opposite of a property write where clamping would silently do something other
/// than what was asked.
constexpr float kMinZoom = 0.05f;
constexpr float kMaxZoom = 4.f;

EditorUI* editorUi() {
    auto* editor = LevelEditorLayer::get();
    return editor == nullptr ? nullptr : editor->m_editorUI;
}

cocos2d::CCPoint viewCentre() {
    return cocos2d::CCDirector::sharedDirector()->getWinSize() * 0.5f;
}

/// The centre of the view in level coordinates.
///
/// Asked of the object layer's own transform rather than read off its position: objects
/// live at level coordinates *inside* that layer, so converting the screen centre into its
/// node space answers "which level point is the middle of my screen showing?" directly,
/// and it stays correct at any zoom (a position-minus-half-a-window calculation only does
/// at 1x, because zoom scales the layer as well as moving it).
///
/// Reported with the caveat that this is a measurement, not a documented accessor: the
/// editor has no `getCameraPosition`, and the earlier attempt to read the ground layer's
/// position for a similar purpose turned out to track the camera rather than describe the
/// level (SPEC.md 0.1). This one is used to *report* where the view is, never to place
/// objects, so a slightly surprising value costs nothing.
Camera measureCamera(EditorUI* ui, LevelEditorLayer* editor) {
    Camera camera;

    camera.zoom = ui->m_editorZoom > 0.f ? ui->m_editorZoom : 1.f;

    if (editor->m_objectLayer != nullptr) {
        auto const centre = editor->m_objectLayer->convertToNodeSpace(viewCentre());
        camera.x = centre.x;
        camera.y = centre.y;
    }
    return camera;
}

} // namespace

geode::Result<Camera> readCamera() {
    auto* ui = editorUi();
    auto* editor = LevelEditorLayer::get();
    if (ui == nullptr || editor == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }
    return Ok(measureCamera(ui, editor));
}

geode::Result<Camera> writeCamera(
    std::optional<double> x, std::optional<double> y, std::optional<float> zoom
) {
    auto* ui = editorUi();
    auto* editor = LevelEditorLayer::get();
    if (ui == nullptr || editor == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }

    auto const before = measureCamera(ui, editor);

    if (x.has_value() || y.has_value()) {
        // "Centre here": a missing component keeps the current one, so panning sideways
        // does not also jump vertically.
        auto const wanted = cocos2d::CCPoint {
            static_cast<float>(x.value_or(before.x)),
            static_cast<float>(y.value_or(before.y)),
        };
        if (editor->m_objectLayer != nullptr) {
            // The layer must travel by however far the wanted point is from the screen
            // centre *in screen points*, so the delta is measured in world space: at 4x
            // zoom the level moves four times as far per point as at 1x, and asking for a
            // node-space delta would overshoot accordingly.
            auto const onScreen = editor->m_objectLayer->convertToWorldSpace(wanted);
            // `moveGamelayer`, not a position write: it also re-syncs the grid layers and
            // the edge markers that the editor keeps outside the object layer.
            ui->moveGamelayer(cocos2d::CCPoint { viewCentre() - onScreen });
            ui->constrainGameLayerPosition();
        }
    }

    if (zoom.has_value()) {
        auto const clamped = std::clamp(*zoom, kMinZoom, kMaxZoom);
        if (std::abs(clamped - *zoom) > 0.001f) {
            log::info("camera: zoom {} clamped to {}", *zoom, clamped);
        }
        // The two-argument overload zooms around a given point; the single-argument one
        // uses wherever the editor last zoomed around, which would drift the view.
        ui->updateZoom(clamped, viewCentre());
    }

    auto const after = measureCamera(ui, editor);
    log::info(
        "camera: ({}, {}) zoom {} -> ({}, {}) zoom {}",
        before.x, before.y, before.zoom, after.x, after.y, after.zoom
    );
    return Ok(after);
}

matjson::Value cameraToJson(Camera const& camera) {
    auto result = matjson::Value::object();
    result["x"] = camera.x;
    result["y"] = camera.y;
    result["zoom"] = static_cast<double>(camera.zoom);
    return result;
}

} // namespace gdai::editor
