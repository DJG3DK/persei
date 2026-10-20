//! Quitting. The tray's Quit, and the window's close button when the person
//! has chosen it, shut Persei down: the stack's containers stop, and so
//! does Docker Desktop when this app is what started it. Until 2026-10-03
//! Quit left the stack running ("the stack keeps running"), and the
//! containers went on holding gigabytes of memory with no window left to
//! say so. An update's restart is not a quit and leaves the stack alone.
//!
//! The decisions are here as plain functions so each is a table test; the
//! docker calls and the dialogs live in lib.rs.

use serde::{Deserialize, Serialize};

use crate::update::Agent;

/// What the window's close button does.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CloseAction {
    /// Not chosen yet: the first close asks, and the answer is kept.
    #[default]
    Ask,
    /// Hide the window; the app and the stack keep running in the tray.
    Tray,
    /// Quit: stop the stack and the app.
    Quit,
}

impl CloseAction {
    /// The panel's value for it, the same words the preferences file uses.
    pub fn parse(s: &str) -> Option<CloseAction> {
        match s {
            "ask" => Some(CloseAction::Ask),
            "tray" => Some(CloseAction::Tray),
            "quit" => Some(CloseAction::Quit),
            _ => None,
        }
    }
}

/// What a quit does, decided before anything is stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QuitPlan {
    /// A task is running: ask before stopping it.
    pub confirm: bool,
    /// Stop the stack's containers.
    pub stop_stack: bool,
    /// Stop Docker Desktop too.
    pub stop_docker: bool,
}

/// `installed`: a stack exists on this machine. `started_docker`: this app
/// started Docker Desktop in this session. Docker Desktop that was already
/// running, or starts with Windows, is the person's and is left alone.
pub fn plan(agent: Agent, installed: bool, started_docker: bool) -> QuitPlan {
    QuitPlan {
        confirm: agent == Agent::Busy,
        // Down still stops: "nothing answered" can be a stack starting up,
        // and `compose stop` on stopped containers is a no-op.
        stop_stack: installed,
        stop_docker: started_docker,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_busy_agent_is_asked_about_before_anything_stops() {
        assert!(plan(Agent::Busy, true, false).confirm);
        assert!(!plan(Agent::Idle, true, false).confirm);
        assert!(!plan(Agent::Down, true, false).confirm);
    }

    #[test]
    fn the_stack_stops_whenever_one_is_installed() {
        for agent in [Agent::Busy, Agent::Idle, Agent::Down] {
            assert!(plan(agent, true, false).stop_stack);
            assert!(!plan(agent, false, false).stop_stack);
        }
    }

    #[test]
    fn docker_desktop_stops_only_when_this_app_started_it() {
        assert!(plan(Agent::Idle, true, true).stop_docker);
        assert!(!plan(Agent::Idle, true, false).stop_docker);
    }

    #[test]
    fn the_close_choice_round_trips_through_the_preferences_file() {
        for (word, action) in [
            ("ask", CloseAction::Ask),
            ("tray", CloseAction::Tray),
            ("quit", CloseAction::Quit),
        ] {
            assert_eq!(CloseAction::parse(word), Some(action));
            assert_eq!(
                serde_json::to_string(&action).unwrap(),
                format!("\"{word}\"")
            );
        }
        assert_eq!(CloseAction::parse("later"), None);
        assert_eq!(CloseAction::default(), CloseAction::Ask);
    }
}
