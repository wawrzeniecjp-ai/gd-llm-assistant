#include "replay.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/PlayLayer.hpp>
#include <Geode/binding/PlayerObject.hpp>
#include <Geode/modify/EditorUI.hpp>
#include <Geode/modify/GJBaseGameLayer.hpp>
#include <Geode/modify/LevelEditorLayer.hpp>
#include <Geode/modify/PlayerObject.hpp>

#include <fmt/format.h>

#include <algorithm>
#include <chrono>
#include <limits>
#include <optional>
#include <string>
#include <vector>

#include "../editor/properties.hpp"
#include "../editor/transaction.hpp"
#include "../ui/chat_panel.hpp"

using namespace geode::prelude;

namespace gdai::exec {
namespace {

constexpr int kMaxTicks = 14400;      // 60 s at 240 Hz
constexpr int kMaxInputs = 2000;
constexpr int kMaxSamples = 6000;     // keeps a columnar trace well under 1 MB
constexpr double kNoTickGiveUpSeconds = 3.0;

double nowSeconds() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

struct ScriptedInput {
    int tick = 0;
    bool down = true;
    bool player1 = true;
};

/// Columnar trace: one vector per field, so a long run serialises compactly.
struct Trace {
    std::vector<int> tick;
    std::vector<double> x, y, vy, speed;
    std::vector<int> grounded, gravity, mini;
    std::vector<std::string> mode;

    std::size_t size() const { return tick.size(); }

    matjson::Value toJson() const {
        auto out = matjson::Value::object();
        auto ints = [](std::vector<int> const& v) {
            auto a = matjson::Value::array();
            for (auto i : v) a.push(i);
            return a;
        };
        auto doubles = [](std::vector<double> const& v) {
            auto a = matjson::Value::array();
            for (auto d : v) a.push(std::round(d * 1000.0) / 1000.0);
            return a;
        };
        out["tick"] = ints(tick);
        out["x"] = doubles(x);
        out["y"] = doubles(y);
        out["vy"] = doubles(vy);
        out["speed"] = doubles(speed);
        out["grounded"] = ints(grounded);
        out["gravity"] = ints(gravity);
        out["mini"] = ints(mini);
        auto modes = matjson::Value::array();
        for (auto const& m : mode) modes.push(m);
        out["mode"] = modes;
        return out;
    }
};

char const* modeOf(PlayerObject* p) {
    if (p->m_isShip) return "ship";
    if (p->m_isBird) return "ufo";
    if (p->m_isBall) return "ball";
    if (p->m_isDart) return "wave";
    if (p->m_isRobot) return "robot";
    if (p->m_isSpider) return "spider";
    if (p->m_isSwing) return "swing";
    return "cube";
}

/// The one replay that can be running. Main thread only: the job and the tick hook
/// both run there, so no locking is needed.
struct Replay {
    enum class State { Idle, Running, Finished, Cancelled };

    State state = State::Idle;
    std::vector<ScriptedInput> inputs;   // sorted by tick
    std::size_t nextInput = 0;
    int maxTicks = 0;
    int sampleEvery = 1;

    int ticks = 0;                       // processCommands calls counted so far
    bool injecting = false;
    std::string outcome;                 // "death", "max_ticks"
    std::string cancelReason;
    matjson::Value death = matjson::Value(nullptr);
    Trace trace;
    bool traceTruncated = false;

    // S-L3 diagnostics.
    double startedAt = 0.0;
    double firstTickAt = 0.0;
    double lastTickAt = 0.0;
    int updateCalls = 0;
    int halfTicks = 0;
    int lastTicks = 0;
    double dtMin = std::numeric_limits<double>::max();
    double dtMax = 0.0;
    double dtSum = 0.0;
    std::optional<unsigned> progressFirst;
    unsigned progressLast = 0;
    matjson::Value applied = matjson::Value::array();

    // Where the per-tick state lives: the first kProbeTicks ticks log every player
    // update / collision call in order, with three candidate positions each time.
    bool inTick = false;
    /// Inside any part of a physics step: the player update, collisions, or commands.
    /// Death happens in the player's collision step, which is outside processCommands.
    int inPhysics = 0;
    bool stoppingOurselves = false;
    int playerUpdates = 0;
    int postCollisions = 0;
    matjson::Value probes = matjson::Value::array();
    matjson::Value stops = matjson::Value::array();
    std::string deathSource;

    static Replay& get() {
        static Replay instance;
        return instance;
    }

    void reset() { *this = Replay{}; }

    bool running() const { return state == State::Running; }

    void sample(GJBaseGameLayer* layer, bool force) {
        auto* p = layer->m_player1;
        if (p == nullptr) return;
        if (!force && ticks % sampleEvery != 0) return;
        if (trace.size() >= static_cast<std::size_t>(kMaxSamples)) {
            traceTruncated = true;
            return;
        }
        // `getRealPosition`, not `m_position`: the S-L3 probes showed `m_position` is a
        // copy that lags and only catches up every other tick, while the real position
        // moves 1.298 units (1x) on every tick.
        auto const real = p->getRealPosition();
        trace.tick.push_back(ticks);
        trace.x.push_back(real.x);
        trace.y.push_back(editor::cocosYToLevel(real.y));
        trace.vy.push_back(p->m_yVelocity);
        trace.speed.push_back(p->m_playerSpeed);
        trace.grounded.push_back(p->m_isOnGround ? 1 : 0);
        trace.gravity.push_back(p->m_isUpsideDown ? -1 : 1);
        trace.mini.push_back(p->m_vehicleSize < 1.0f ? 1 : 0);
        trace.mode.push_back(modeOf(p));
    }

    static constexpr int kProbeTicks = 40;

    void probe(char const* event, PlayerObject* p, double dt = -1.0, int flag = -1) {
        if (p == nullptr || ticks >= kProbeTicks) return;
        auto e = matjson::Value::object();
        e["t"] = ticks;
        e["e"] = event;
        e["m_x"] = p->m_position.x;
        e["m_y"] = p->m_position.y;
        auto const real = p->getRealPosition();
        e["real_x"] = real.x;
        e["node_x"] = p->getPositionX();
        e["vy"] = p->m_yVelocity;
        if (dt >= 0) e["dt"] = dt;
        if (flag >= 0) e["flag"] = flag;
        probes.push(e);
    }

    /// Records a death, once. The cause may be null when the game did not say.
    void captureDeath(GJBaseGameLayer* layer, std::string_view source) {
        if (state != State::Running) return;
        auto* p = layer->m_player1;
        sample(layer, true);
        auto info = matjson::Value::object();
        info["tick"] = ticks;
        if (p != nullptr) {
            auto const real = p->getRealPosition();
            info["x"] = real.x;
            info["y"] = editor::cocosYToLevel(real.y);
            auto* cause = p->m_collidedObject;
            info["cause_uid"] = cause != nullptr ? matjson::Value(cause->m_uniqueID)
                                                 : matjson::Value(nullptr);
            info["cause_id"] = cause != nullptr ? matjson::Value(cause->m_objectID)
                                                : matjson::Value(nullptr);
        }
        death = info;
        deathSource = std::string(source);
        outcome = "death";
        state = State::Finished;
    }

    /// A stop of the playtest we did not ask for. Inside a tick it is the game ending
    /// the run (death); between ticks it is the user.
    void externalStop(GJBaseGameLayer* layer, char const* where) {
        auto s = matjson::Value::object();
        s["fn"] = where;
        s["tick"] = ticks;
        s["in_tick"] = inTick;
        s["in_physics"] = inPhysics > 0;
        auto* p = layer->m_player1;
        s["player_dead"] = p != nullptr && p->m_isDead;
        s["layer_player_died"] = layer->m_playerDied;
        s["ours"] = stoppingOurselves;
        stops.push(s);
        if (stoppingOurselves || state != State::Running) return;
        bool const died = inPhysics > 0 || (p != nullptr && p->m_isDead) || layer->m_playerDied;
        if (died) {
            captureDeath(layer, where);
        } else {
            cancelReason = fmt::format("the playtest was stopped ({}) at tick {}", where, ticks);
            state = State::Cancelled;
        }
    }

    void beforeTick(GJBaseGameLayer* layer, float dt, bool isHalfTick, bool isLastTick) {
        auto const now = nowSeconds();
        if (ticks == 0) {
            firstTickAt = now;
            sample(layer, true);  // the state before the first tick, as tick 0
        }
        lastTickAt = now;
        dtMin = std::min(dtMin, static_cast<double>(dt));
        dtMax = std::max(dtMax, static_cast<double>(dt));
        dtSum += dt;
        if (isHalfTick) ++halfTicks;
        if (isLastTick) ++lastTicks;
        auto const progress = layer->m_gameState.m_currentProgress;
        if (!progressFirst) progressFirst = progress;
        progressLast = progress;

        // Inputs for this tick act on the step that runs now.
        while (nextInput < inputs.size() && inputs[nextInput].tick <= ticks) {
            auto const& input = inputs[nextInput++];
            injecting = true;
            layer->handleButton(input.down, 1, input.player1);
            injecting = false;
            auto record = matjson::Value::object();
            record["tick"] = ticks;
            record["action"] = input.down ? "press" : "release";
            record["progress"] = static_cast<int>(progress);
            applied.push(record);
        }
    }

    void afterTick(GJBaseGameLayer* layer) {
        ++ticks;
        auto* p = layer->m_player1;
        bool const dead = (p != nullptr && p->m_isDead) || layer->m_playerDied;
        if (dead) {
            captureDeath(layer, p != nullptr && p->m_isDead ? "m_isDead" : "m_playerDied");
            return;
        }
        sample(layer, false);
        if (ticks >= maxTicks) {
            outcome = "max_ticks";
            state = State::Finished;
        }
    }

    matjson::Value diagnostics() const {
        auto d = matjson::Value::object();
        d["process_commands_calls"] = ticks;
        d["update_calls"] = updateCalls;
        d["half_ticks"] = halfTicks;
        d["last_ticks"] = lastTicks;
        d["dt_min"] = ticks ? dtMin : 0.0;
        d["dt_max"] = dtMax;
        d["dt_mean"] = ticks ? dtSum / ticks : 0.0;
        d["progress_first"] = progressFirst ? static_cast<int>(*progressFirst) : -1;
        d["progress_last"] = static_cast<int>(progressLast);
        d["seconds_to_first_tick"] = firstTickAt > 0 ? firstTickAt - startedAt : -1.0;
        d["tick_seconds"] = lastTickAt > firstTickAt ? lastTickAt - firstTickAt : 0.0;
        d["inputs_applied"] = applied;
        d["player_update_calls"] = playerUpdates;
        d["post_collision_calls"] = postCollisions;
        d["probes"] = probes;
        d["stops"] = stops;
        d["death_source"] = deathSource;
        return d;
    }
};

} // namespace

// The tick hook. `processCommands` is the game's fixed-step entry (Windows 0x342870);
// whether it really runs once per physics tick in the editor's playtest is what the
// diagnostics establish. Only the editor's own layer is touched, and only while a
// replay is running, so normal play and PlayLayer playtests are unaffected.
class $modify(ReplayTickHook, GJBaseGameLayer) {
    void processCommands(float dt, bool isHalfTick, bool isLastTick) {
        auto& replay = Replay::get();
        bool const ours = replay.running()
            && static_cast<GJBaseGameLayer*>(LevelEditorLayer::get()) == this;
        if (ours) {
            replay.beforeTick(this, dt, isHalfTick, isLastTick);
            replay.probe("tick_begin", m_player1, dt);
            replay.inTick = true;
            ++replay.inPhysics;
        }
        GJBaseGameLayer::processCommands(dt, isHalfTick, isLastTick);
        if (ours) {
            replay.inTick = false;
            --replay.inPhysics;
            if (replay.running()) {
                replay.probe("tick_end", m_player1);
                replay.afterTick(this);
            }
        }
    }

    void update(float dt) {
        auto& replay = Replay::get();
        if (replay.running() && static_cast<GJBaseGameLayer*>(LevelEditorLayer::get()) == this) {
            ++replay.updateCalls;
        }
        GJBaseGameLayer::update(dt);
    }

    int checkCollisions(PlayerObject* player, float dt, bool ignoreDamage) {
        auto& replay = Replay::get();
        bool const ours = replay.running()
            && static_cast<GJBaseGameLayer*>(LevelEditorLayer::get()) == this;
        if (ours) ++replay.inPhysics;
        auto const result = GJBaseGameLayer::checkCollisions(player, dt, ignoreDamage);
        if (ours) --replay.inPhysics;
        return result;
    }

    // A press we did not make means the user has taken over: the run is no longer the
    // script, so it is cancelled rather than reported (LEVEL-DESIGN-SPEC.md 7.2.1).
    void handleButton(bool down, int button, bool isPlayer1) {
        auto& replay = Replay::get();
        if (replay.running() && !replay.injecting && down) {
            replay.cancelReason = "the user pressed a button during the replay";
            replay.state = Replay::State::Cancelled;
        }
        GJBaseGameLayer::handleButton(down, button, isPlayer1);
    }
};

// Instrumentation for S-L3: which player calls happen inside a tick, and in what order.
class $modify(ReplayPlayerHook, PlayerObject) {
    bool isReplayPlayer() {
        auto* layer = LevelEditorLayer::get();
        return Replay::get().running() && layer != nullptr && layer->m_player1 == this;
    }

    void update(float dt) {
        bool const ours = isReplayPlayer();
        if (ours) {
            ++Replay::get().playerUpdates;
            Replay::get().probe("player_update_begin", this, dt);
            ++Replay::get().inPhysics;
        }
        PlayerObject::update(dt);
        if (ours) {
            --Replay::get().inPhysics;
            Replay::get().probe("player_update_end", this, dt);
        }
    }

    void postCollision(float dt, bool betweenSteps) {
        bool const ours = isReplayPlayer();
        if (ours) {
            ++Replay::get().postCollisions;
            ++Replay::get().inPhysics;
        }
        PlayerObject::postCollision(dt, betweenSteps);
        if (ours) {
            --Replay::get().inPhysics;
            Replay::get().probe("post_collision", this, dt, betweenSteps ? 1 : 0);
        }
    }
};

// Death ends the editor's playtest by itself, and the reset that follows clears the
// player's state: the first run saw the playtest stop and could only report CANCELLED.
// So the stop paths are watched, and a stop that lands inside a tick is read as the
// death it is, with the player captured before the reset.
class $modify(ReplayStopEditorHook, LevelEditorLayer) {
    void onStopPlaytest() {
        Replay::get().externalStop(this, "LevelEditorLayer::onStopPlaytest");
        LevelEditorLayer::onStopPlaytest();
    }
};

class $modify(ReplayStopUiHook, EditorUI) {
    void onStopPlaytest(cocos2d::CCObject* sender) {
        if (auto* layer = LevelEditorLayer::get()) {
            Replay::get().externalStop(layer, "EditorUI::onStopPlaytest");
        }
        EditorUI::onStopPlaytest(sender);
    }

    void playtestStopped() {
        if (auto* layer = LevelEditorLayer::get()) {
            Replay::get().externalStop(layer, "EditorUI::playtestStopped");
        }
        EditorUI::playtestStopped();
    }
};

namespace {

class FailedReplay final : public Job {
public:
    FailedReplay(std::string code, std::string message)
        : m_code(std::move(code)), m_message(std::move(message)) {}
    bool step() override { return true; }
    JobOutcome takeOutcome() override { return JobOutcome::failure(m_code, m_message); }
    std::string_view type() const override { return "run_replay"; }

private:
    std::string m_code;
    std::string m_message;
};

class ReplayJob final : public Job {
public:
    ReplayJob(std::vector<ScriptedInput> inputs, int maxTicks, int sampleEvery)
        : m_inputs(std::move(inputs)), m_maxTicks(maxTicks), m_sampleEvery(sampleEvery) {}

    std::string_view type() const override { return "run_replay"; }

    bool step() override {
        auto* level = LevelEditorLayer::get();
        auto* ui = level != nullptr ? level->m_editorUI : nullptr;
        auto& replay = Replay::get();

        if (!m_started) {
            m_started = true;
            if (ui == nullptr) {
                return fail("EDITOR_NOT_OPEN", "no editor is open");
            }
            if (level->m_playbackMode != PlaybackMode::Not || PlayLayer::get() != nullptr) {
                return fail("EDITOR_BUSY", "the level is already being played; stop it first");
            }
            if (editor::TransactionManager::get().isOpen()) {
                return fail("EDITOR_BUSY",
                            "a transaction is open; end it before running a replay");
            }
            if (replay.state != Replay::State::Idle) {
                return fail("EDITOR_BUSY", "another replay is running");
            }
            replay.reset();
            replay.inputs = m_inputs;
            replay.maxTicks = m_maxTicks;
            replay.sampleEvery = m_sampleEvery;
            replay.startedAt = nowSeconds();
            replay.state = Replay::State::Running;
            log::info("run_replay: starting, {} inputs, max {} ticks", m_inputs.size(), m_maxTicks);
            // A message sent a moment ago would otherwise have the panel swallow this.
            ui::ChatPanel::clearPlaytestSuppression();
            ui->onPlaytest(nullptr);
            if (level->m_playbackMode != PlaybackMode::Playing) {
                replay.state = Replay::State::Idle;
                return fail("EDITOR_BUSY", "the editor's playtest did not start");
            }
            return false;
        }

        auto const elapsed = nowSeconds() - replay.startedAt;
        if (replay.state == Replay::State::Running) {
            if (level == nullptr || level->m_playbackMode != PlaybackMode::Playing) {
                replay.state = Replay::State::Cancelled;
                replay.cancelReason = "the playtest was stopped before the run finished";
            } else if (replay.ticks == 0 && elapsed > kNoTickGiveUpSeconds) {
                // The spike's most important negative answer: the hook never fired.
                replay.outcome = "no_ticks";
                replay.state = Replay::State::Finished;
            } else if (elapsed > m_maxTicks / 240.0 * 4.0 + 10.0) {
                replay.outcome = "stalled";
                replay.state = Replay::State::Finished;
            } else {
                return false;
            }
        }

        if (ui != nullptr && level->m_playbackMode != PlaybackMode::Not) {
            replay.stoppingOurselves = true;
            ui->onStopPlaytest(nullptr);
            replay.stoppingOurselves = false;
        }
        auto const wall = nowSeconds() - replay.startedAt;

        if (replay.state == Replay::State::Cancelled) {
            log::info("run_replay: cancelled after {} ticks: {}", replay.ticks, replay.cancelReason);
            m_outcome = JobOutcome::failure("CANCELLED", replay.cancelReason);
            replay.state = Replay::State::Idle;
            return true;
        }

        auto result = matjson::Value::object();
        result["outcome"] = replay.outcome;
        result["ticks"] = replay.ticks;
        result["death"] = replay.death;
        result["trace"] = replay.trace.toJson();
        result["trace_truncated"] = replay.traceTruncated;
        auto const tickSeconds = replay.lastTickAt - replay.firstTickAt;
        result["tick_rate_observed"] = tickSeconds > 0 && replay.ticks > 1
            ? (replay.ticks - 1) / tickSeconds : 0.0;
        result["time_scale"] = 1.0;
        auto diagnostics = replay.diagnostics();
        diagnostics["wall_seconds"] = wall;
        result["diagnostics"] = diagnostics;
        log::info("run_replay: {} after {} ticks in {:.2f}s ({} update calls)",
                  replay.outcome, replay.ticks, wall, replay.updateCalls);
        m_outcome = JobOutcome::success(std::move(result));
        replay.state = Replay::State::Idle;
        return true;
    }

    JobOutcome takeOutcome() override { return std::move(m_outcome); }

private:
    bool fail(std::string_view code, std::string_view message) {
        m_outcome = JobOutcome::failure(code, message);
        return true;
    }

    std::vector<ScriptedInput> m_inputs;
    int m_maxTicks;
    int m_sampleEvery;
    bool m_started = false;
    JobOutcome m_outcome;
};

std::optional<int> intField(matjson::Value const& obj, char const* key, int fallback) {
    if (!obj.contains(key)) return fallback;
    auto value = obj[key].asInt();
    if (!value) return std::nullopt;
    return static_cast<int>(value.unwrap());
}

} // namespace

std::unique_ptr<Job> makeReplayJob(matjson::Value const& payload) {
    auto invalid = [](std::string message) -> std::unique_ptr<Job> {
        return std::make_unique<FailedReplay>("INVALID_REQUEST", std::move(message));
    };

    if (payload.contains("start")) {
        auto kind = payload["start"]["kind"].asString().unwrapOr("level_start");
        if (kind != "level_start") {
            return std::make_unique<FailedReplay>(
                "UNSUPPORTED_REQUEST", "only start.kind 'level_start' is implemented so far");
        }
    }
    auto maxTicks = intField(payload, "max_ticks", 2400);
    if (!maxTicks || *maxTicks < 1 || *maxTicks > kMaxTicks) {
        return invalid(fmt::format("'max_ticks' must be an integer from 1 to {}", kMaxTicks));
    }
    auto sampleEvery = intField(payload, "sample_every", 1);
    if (!sampleEvery || *sampleEvery < 1 || *sampleEvery > 240) {
        return invalid("'sample_every' must be an integer from 1 to 240");
    }

    std::vector<ScriptedInput> inputs;
    if (payload.contains("inputs")) {
        if (!payload["inputs"].isArray()) {
            return invalid("'inputs' must be an array");
        }
        for (auto const& item : payload["inputs"]) {
            if (inputs.size() >= static_cast<std::size_t>(kMaxInputs)) {
                return invalid(fmt::format("at most {} inputs", kMaxInputs));
            }
            auto tick = item["tick"].asInt();
            auto action = item["action"].asString();
            if (!tick || tick.unwrap() < 0 || !action
                || (action.unwrap() != "press" && action.unwrap() != "release")) {
                return invalid("each input needs a non-negative 'tick' and an 'action' of "
                               "'press' or 'release'");
            }
            auto player = item.contains("player") ? item["player"].asInt().unwrapOr(1) : 1;
            inputs.push_back({static_cast<int>(tick.unwrap()), action.unwrap() == "press",
                              player != 2});
        }
    }
    std::stable_sort(inputs.begin(), inputs.end(),
                     [](auto const& a, auto const& b) { return a.tick < b.tick; });
    return std::make_unique<ReplayJob>(std::move(inputs), *maxTicks, *sampleEvery);
}

} // namespace gdai::exec
