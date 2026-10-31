#include "panel_host.hpp"

using namespace geode::prelude;

namespace gdai::ui {

PanelHost& PanelHost::get() {
    static PanelHost instance;
    return instance;
}

// ---------------------------------------------------------------------------
// Main thread: panel lifetime
// ---------------------------------------------------------------------------

ChatPanel* PanelHost::attach(LevelEditorLayer* editor) {
    if (m_panel) {
        return m_panel;
    }
    if (!editor || !editor->m_editorUI) {
        // `m_editorUI` is assigned after `LevelEditorLayer::init` returns, so
        // the caller polls. Null here means "not yet", not "failed".
        return nullptr;
    }

    auto panel = ChatPanel::create();
    if (!panel) {
        log::error("panel: creation failed");
        return nullptr;
    }

    auto winSize = cocos2d::CCDirector::sharedDirector()->getWinSize();
    panel->setPosition({
        winSize.width - panel->getContentSize().width - 8.f,
        8.f,
    });

    // The handlers are re-bound here rather than captured once, so that a
    // handler installed after this panel was created still takes effect.
    panel->setOnSubmit([this](std::string text) {
        if (m_onSubmit) {
            m_onSubmit(std::move(text));
        }
    });
    panel->setOnCancel([this] {
        if (m_onCancel) {
            m_onCancel();
        }
    });
    panel->setOnRevert([this](std::string txnId) {
        if (m_onRevert) {
            m_onRevert(std::move(txnId));
        }
    });

    // Above the editor's own chrome so its controls are reachable. This is not
    // what decides input routing -- the panel's children register their own
    // touch handlers (spike S6).
    editor->m_editorUI->addChild(panel, 1000);
    m_panel = panel;

    // Replay whatever was posted while there was no panel to post it to.
    if (m_lastStatus) {
        panel->setStatus(*m_lastStatus);
    }
    if (!m_lastSystemText.empty()) {
        panel->addMessage("system", m_lastSystemText);
    }

    // Re-apply the user's hide choice. The panel is rebuilt on every editor open, so
    // without this a user who hides it would find it back after closing and reopening
    // a level -- which reads as the setting not having worked.
    if (m_hidden) {
        panel->setHidden(true);
    }
    // Same for playtest: the flag can be set while no panel exists, because the hook
    // that sets it runs on the editor's actions rather than on the panel's life.
    if (m_playtestHidden) {
        panel->setPlaytestHidden(true);
    }

    log::info(
        "panel: attached at ({}, {}), win size {}x{}",
        panel->getPositionX(), panel->getPositionY(), winSize.width, winSize.height
    );
    return m_panel;
}

void PanelHost::detach() {
    if (!m_panel) return;
    m_panel->removeFromParent();
    m_panel = nullptr;
    log::info("panel: detached");
}

void PanelHost::setHidden(bool hidden) {
    m_hidden = hidden;
    if (m_panel) {
        m_panel->setHidden(hidden);
    }
}

void PanelHost::setPlaytestHidden(bool hidden) {
    // Remembered even with no panel attached: the editor's playtest hooks can fire
    // while the panel is being rebuilt, and forgetting the flag there would leave the
    // panel on screen for the rest of the playtest.
    m_playtestHidden = hidden;
    if (m_panel) {
        m_panel->setPlaytestHidden(hidden);
    }
}

// ---------------------------------------------------------------------------
// Any thread: posting
// ---------------------------------------------------------------------------

void PanelHost::onPanel(geode::Function<void(ChatPanel&)> fn) {
    // `mutable`, because `geode::Function` is a `std::move_only_function` and
    // calling it is a non-const operation, while a capturing lambda's call
    // operator is const by default.
    geode::queueInMainThread([this, fn = std::move(fn)]() mutable {
        // Re-checked inside the hop: the editor can close between the call and
        // the frame that runs this, and `detach` nulls the pointer for exactly
        // that reason.
        if (m_panel) {
            fn(*m_panel);
        }
    });
}

void PanelHost::postStatus(Status status) {
    // Recorded inside the hop, so both fields are touched only on the main
    // thread and no lock is needed.
    geode::queueInMainThread([this, status] {
        m_lastStatus = status;
        if (m_panel) {
            m_panel->setStatus(status);
        }
    });
}

void PanelHost::postSystem(std::string text) {
    geode::queueInMainThread([this, text = std::move(text)] {
        m_lastSystemText = text;
        if (m_panel) {
            m_panel->addMessage("system", text);
        }
    });
}

void PanelHost::postError(std::string text) {
    onPanel([text = std::move(text)](ChatPanel& panel) { panel.addMessage("error", text); });
}

void PanelHost::postUserMessage(std::string text) {
    onPanel([text = std::move(text)](ChatPanel& panel) { panel.addMessage("user", text); });
}

void PanelHost::postAssistantMessage(std::string const& messageId, std::string text) {
    onPanel([messageId, text = std::move(text)](ChatPanel& panel) {
        // The response replaces the thinking indicator (SPEC.md 8.9).
        panel.setThinking(false);
        panel.upsertMessage(messageId, "assistant", text);
    });
}

void PanelHost::postAssistantDelta(std::string const& messageId, std::string delta) {
    onPanel([messageId, delta = std::move(delta)](ChatPanel& panel) {
        panel.setThinking(false);
        panel.appendMessageDelta(messageId, delta);
    });
}

void PanelHost::postTurnStarted() {
    onPanel([](ChatPanel& panel) {
        panel.setInputEnabled(true);
        panel.setProcessing(true);
        panel.setThinking(true);
    });
}

void PanelHost::postProgress(std::string toolName) {
    onPanel([toolName = std::move(toolName)](ChatPanel& panel) {
        // The thinking indicator may not exist yet if the event arrives before
        // `turn_started`; `setProgress` copes with that and the name is picked up when
        // the indicator is created.
        panel.setThinking(true);
        panel.setProgress(std::move(toolName));
    });
}

void PanelHost::postTurnFinished(
    std::string status, std::vector<std::string> lines, std::string txnId
) {
    onPanel(
        [status = std::move(status), lines = std::move(lines), txnId = std::move(txnId)](
            ChatPanel& panel
        ) {
            panel.setProcessing(false);
            panel.setThinking(false);
            panel.setInputEnabled(true);
            panel.showTurnSummary(status, lines, txnId);
        }
    );
}

void PanelHost::postRevertFinished(bool ok, std::string message) {
    onPanel([ok, message = std::move(message)](ChatPanel& panel) {
        panel.finishRevert(ok, message);
    });
}

void PanelHost::postClear() {
    geode::queueInMainThread([this] {
        if (!m_panel) {
            return;
        }
        m_panel->clearMessages();
        // The sidecar's history replaces the *conversation*, not the panel's own
        // status. Re-stating the connection line afterwards is what stops the
        // history frame -- which arrives immediately after the handshake, while
        // the panel is still being built -- from wiping the "Connected." message
        // posted a moment before it.
        if (!m_lastSystemText.empty()) {
            m_panel->addMessage("system", m_lastSystemText);
        }
    });
}

void PanelHost::setSubmitHandler(geode::Function<void(std::string)> onSubmit) {
    m_onSubmit = std::move(onSubmit);
}

void PanelHost::setCancelHandler(geode::Function<void()> onCancel) {
    m_onCancel = std::move(onCancel);
}

void PanelHost::setRevertHandler(geode::Function<void(std::string)> onRevert) {
    m_onRevert = std::move(onRevert);
}

} // namespace gdai::ui
