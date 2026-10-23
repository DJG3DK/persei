//! What a pass over the stack does, decided from what it sees. Nothing in
//! here runs docker or asks the network, so every decision is a table test;
//! stack.rs gathers the facts and carries the decision out.

use crate::stack::{image_ref, local_name, newer_than, record_is_proven, Version, IMAGES};
use serde::Deserialize;
use std::collections::BTreeMap;

/// The agent, as the health route answered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Agent {
    /// Nothing answered: the stack is down, or not yet up.
    Down,
    /// Answered, with a task in flight, or too old to say (busy_in).
    Busy,
    /// Answered, nothing in flight.
    Idle,
}

/// What the stack knows before it decides.
pub struct Seen<'a> {
    /// The release this app was built from (stack::release_tag).
    pub app_tag: &'a str,
    /// version.json, if there is one.
    pub record: Option<&'a Version>,
    /// The agent image Docker has under the local name, if any.
    pub local_id: Option<&'a str>,
    /// The fingerprint of the compose file this app ships.
    pub compose: &'a str,
    pub agent: Agent,
}

/// What to do about the stack this app runs.
#[derive(Debug, PartialEq, Eq)]
pub enum StackMove {
    /// This app's images, started under this app's compose file.
    Current,
    /// Something to do, and a task in flight: not now.
    Busy,
    /// Pull this app's images. Then restart the stack on them when it was
    /// running; a stopped stack starts on them with the next Start.
    Pull { restart: bool },
    /// The images are this app's, but the compose file they were started
    /// under is not this app's. 2026-09-29: an rc16 app ran rc17 images
    /// under rc16's compose file, and the `SANDBOX_*` settings rc17 added
    /// never reached the agent.
    Recompose { restart: bool },
}

pub fn own_release_move(seen: &Seen) -> StackMove {
    let proven = seen
        .record
        .is_some_and(|r| record_is_proven(r, seen.local_id));
    let images_current = proven
        && seen
            .record
            .is_some_and(|r| !newer_than(seen.app_tag, &r.tag));
    let compose_current = seen
        .record
        .is_some_and(|r| r.compose.as_deref() == Some(seen.compose));
    if images_current && compose_current {
        return StackMove::Current;
    }
    if seen.agent == Agent::Busy {
        return StackMove::Busy;
    }
    let restart = seen.agent == Agent::Idle;
    if images_current {
        StackMove::Recompose { restart }
    } else {
        StackMove::Pull { restart }
    }
}

/// Why an automatic pass does nothing, decided before it touches anything.
/// 2026-09-29: the first pass, two minutes after launch, found no record
/// under a first install still pulling, and started a second pull and `up`
/// beside it.
pub fn pass_skip_reason(
    settings_ready: bool,
    installed: bool,
    other_operation: bool,
) -> Option<&'static str> {
    if other_operation {
        return Some("a stack operation is in progress");
    }
    if !settings_ready {
        return Some("the settings are not complete yet");
    }
    if !installed {
        return Some("the stack is not installed yet");
    }
    None
}

/// Whether the health route's answer says a task is in flight. An answer
/// that does not say (an agent older than rc7, which added `busy`; or not
/// JSON at all) counts as busy: a restart on a guess would pull a running
/// task's containers out from under it.
pub fn busy_in(answer: &str) -> bool {
    match serde_json::from_str::<serde_json::Value>(answer)
        .ok()
        .and_then(|v| v.get("busy")?.as_u64())
    {
        Some(count) => count > 0,
        None => true,
    }
}

/// A release's image manifest, `digests.json` beside the installer on the
/// GitHub release: the digest each image was pushed as. Signed with the
/// updater's key (release.yml), so a retagged image on the registry is
/// not what the app pulls.
#[derive(Deserialize, Debug, PartialEq, Eq)]
pub struct Digests {
    pub tag: String,
    pub images: BTreeMap<String, String>,
}

/// Check the manifest against the app's updater key and read it. The key
/// and the signature are base64 of the minisign text, the way the updater
/// keeps them (tauri.conf.json's pubkey; the .sig files tauri signs).
pub fn verified_digests(
    pubkey_b64: &str,
    manifest: &[u8],
    sig_b64: &str,
) -> Result<Digests, String> {
    use base64::Engine;
    let text = |b64: &str, what: &str| -> Result<String, String> {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .map_err(|e| format!("{what} is not base64: {e}"))?;
        String::from_utf8(bytes).map_err(|_| format!("{what} is not text"))
    };
    let key = minisign_verify::PublicKey::decode(&text(pubkey_b64, "the updater key")?)
        .map_err(|e| format!("the updater key does not parse: {e}"))?;
    let sig = minisign_verify::Signature::decode(&text(sig_b64, "the manifest signature")?)
        .map_err(|e| format!("the manifest signature does not parse: {e}"))?;
    key.verify(manifest, &sig, false)
        .map_err(|e| format!("the image manifest is not signed by this app's key: {e}"))?;
    serde_json::from_slice(manifest).map_err(|e| format!("the image manifest does not parse: {e}"))
}

/// The reference to pull for one image: by digest when the release's
/// manifest names one, else by tag (releases before the manifest existed).
pub fn pull_ref(name: &str, tag: &str, digests: Option<&Digests>) -> Result<String, String> {
    match digests {
        None => Ok(image_ref(name, tag)),
        Some(d) => {
            if d.tag != tag {
                return Err(format!(
                    "the image manifest is for {} but {tag} was asked for",
                    d.tag
                ));
            }
            let digest = d
                .images
                .get(name)
                .filter(|s| s.starts_with("sha256:"))
                .ok_or_else(|| {
                    format!("the image manifest for {tag} names no digest for {name}")
                })?;
            Ok(format!(
                "{}@{digest}",
                image_ref(name, tag)
                    .rsplit_once(':')
                    .map_or("", |(repo, _)| repo)
            ))
        }
    }
}

/// The docker commands that bring one release's images in, in order: every
/// pull, then every tag. A failure on the third pull used to leave two
/// images retagged to the new release and two on the old.
pub fn pull_commands(tag: &str, digests: Option<&Digests>) -> Result<Vec<Vec<String>>, String> {
    let refs = IMAGES
        .iter()
        .map(|name| Ok((name, pull_ref(name, tag, digests)?)))
        .collect::<Result<Vec<_>, String>>()?;
    let pulls = refs
        .iter()
        .map(|(_, r)| vec!["pull".to_string(), r.clone()]);
    let tags = refs
        .iter()
        .map(|(name, r)| vec!["tag".to_string(), r.clone(), local_name(name)]);
    Ok(pulls.chain(tags).collect())
}

/// Start needs this app's release, but not the registry: when the pull
/// fails and images are already here, Start goes ahead on them and says so.
/// With no images at all there is nothing to start.
pub fn start_on_local_images(pull_error: &str, images_present: bool) -> Result<String, String> {
    if images_present {
        Ok(format!(
            "{pull_error}. Starting the images already on this machine; the update is tried again later."
        ))
    } else {
        Err(pull_error.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(tag: &str, image_id: Option<&str>, compose: Option<&str>) -> Version {
        Version {
            tag: tag.into(),
            image_id: image_id.map(str::to_string),
            compose: compose.map(str::to_string),
        }
    }

    fn seen<'a>(record: Option<&'a Version>, agent: Agent) -> Seen<'a> {
        Seen {
            app_tag: "v0.9.0-rc17",
            record,
            local_id: Some("sha256:abc"),
            compose: "c17",
            agent,
        }
    }

    #[test]
    fn a_stack_on_this_apps_release_and_compose_file_is_left_alone() {
        let r = record("v0.9.0-rc17", Some("sha256:abc"), Some("c17"));
        for agent in [Agent::Down, Agent::Busy, Agent::Idle] {
            assert_eq!(own_release_move(&seen(Some(&r), agent)), StackMove::Current);
        }
        let ahead = record("v0.9.0-rc18", Some("sha256:abc"), Some("c17"));
        assert_eq!(
            own_release_move(&seen(Some(&ahead), Agent::Idle)),
            StackMove::Current,
            "a stack moved ahead by hand stays there"
        );
    }

    #[test]
    fn an_older_or_unproven_stack_gets_this_apps_images() {
        let older = record("v0.9.0-rc16", Some("sha256:abc"), Some("c17"));
        assert_eq!(
            own_release_move(&seen(Some(&older), Agent::Idle)),
            StackMove::Pull { restart: true }
        );
        assert_eq!(
            own_release_move(&seen(Some(&older), Agent::Down)),
            StackMove::Pull { restart: false },
            "a stack the operator stopped is not started for an update"
        );
        assert_eq!(
            own_release_move(&seen(Some(&older), Agent::Busy)),
            StackMove::Busy
        );
        let unproven = record("v0.9.0-rc17", None, Some("c17"));
        assert_eq!(
            own_release_move(&seen(Some(&unproven), Agent::Idle)),
            StackMove::Pull { restart: true },
            "a record without its image id is not trusted"
        );
        assert_eq!(
            own_release_move(&seen(None, Agent::Down)),
            StackMove::Pull { restart: false },
            "no record: a first install by the pass, not started"
        );
    }

    #[test]
    fn this_apps_images_under_another_compose_file_are_restarted_under_this_one() {
        let stale = record("v0.9.0-rc17", Some("sha256:abc"), Some("c16"));
        assert_eq!(
            own_release_move(&seen(Some(&stale), Agent::Idle)),
            StackMove::Recompose { restart: true }
        );
        assert_eq!(
            own_release_move(&seen(Some(&stale), Agent::Down)),
            StackMove::Recompose { restart: false }
        );
        assert_eq!(
            own_release_move(&seen(Some(&stale), Agent::Busy)),
            StackMove::Busy
        );
        let unstamped = record("v0.9.0-rc17", Some("sha256:abc"), None);
        assert_eq!(
            own_release_move(&seen(Some(&unstamped), Agent::Idle)),
            StackMove::Recompose { restart: true },
            "a record from before the fingerprint was kept"
        );
        let old: Version = serde_json::from_str(r#"{"tag":"v0.9.0-rc13","image_id":"x"}"#).unwrap();
        assert_eq!(old.compose, None, "the old record still parses");
    }

    #[test]
    fn the_pass_waits_for_settings_a_record_and_the_lock() {
        assert_eq!(
            pass_skip_reason(true, true, true),
            Some("a stack operation is in progress")
        );
        assert_eq!(
            pass_skip_reason(false, false, false),
            Some("the settings are not complete yet")
        );
        assert_eq!(
            pass_skip_reason(true, false, false),
            Some("the stack is not installed yet"),
            "a first install still pulling has no record yet"
        );
        assert_eq!(pass_skip_reason(true, true, false), None);
    }

    #[test]
    fn an_agent_that_does_not_say_counts_as_busy() {
        assert!(!busy_in(r#"{"status":"ok","busy":0}"#));
        assert!(busy_in(r#"{"status":"ok","busy":2}"#));
        assert!(
            busy_in(r#"{"status":"ok"}"#),
            "before rc7 the route had no busy field, and the stack was restarted mid-task"
        );
        assert!(busy_in("<html>502</html>"));
        assert!(busy_in(""));
    }

    #[test]
    fn every_image_is_pulled_before_any_is_retagged() {
        let cmds = pull_commands("v0.9.0-rc17", None).unwrap();
        assert_eq!(cmds.len(), 2 * IMAGES.len());
        let (pulls, tags) = cmds.split_at(IMAGES.len());
        assert!(pulls.iter().all(|c| c[0] == "pull"));
        assert!(tags.iter().all(|c| c[0] == "tag"));
        assert_eq!(
            pulls[0],
            vec!["pull", "ghcr.io/djg3dk/tektonix-agent:v0.9.0-rc17"]
        );
        assert_eq!(
            tags[0],
            vec![
                "tag",
                "ghcr.io/djg3dk/tektonix-agent:v0.9.0-rc17",
                "tektonix-agent:latest"
            ]
        );
    }

    // scratch: a key pair and a prehashed minisign signature over MESSAGE,
    // encoded as tauri encodes them. Made by a small script with a fixed
    // seed; the key is a test key and signs nothing else.
    const PUBKEY: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IHRlc3QKUldSWUl1dnVMdVl5S1NkZHJWZXd6VVhOakNpOTBUK3ZHUjhOWFhpazlzRWx3Q0tYRStDQzhveTMK";
    const MESSAGE: &str = r#"{"tag":"v0.9.0-rc23","images":{"agent":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","router":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","reviewer":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","sandbox":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}}"#;
    const SIG: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVSWUl1dnVMdVl5S1lQZGl5dEdhRnoreExsTkpYUmhTYmNQQUxDcWEvTCtNczVoZVBCQzdQaEhpaUcyZmVkZnNQclFsUGwwb04xU1dUcXRLQXJiT2xxQlVVcEpleGdKTHdnPQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxCWZpbGU6ZGlnZXN0cy5qc29uCWhhc2hlZApSL29TdWIvRU9SL3pNRHYzUmxtSURheXBHNFFLS2dWM1FlTVF3eW1aMTVwTU16N3lTKys2S0xSbE85WldVY1kwZmMwUkNtMFY5amx6V2MyK2phQTFEZz09Cg==";

    #[test]
    fn the_image_manifest_is_taken_only_with_its_signature() {
        let d = verified_digests(PUBKEY, MESSAGE.as_bytes(), SIG).unwrap();
        assert_eq!(d.tag, "v0.9.0-rc23");
        assert_eq!(d.images["agent"], format!("sha256:{}", "a".repeat(64)));
        let tampered = MESSAGE.replace("aaaa", "eeee");
        let err = verified_digests(PUBKEY, tampered.as_bytes(), SIG).unwrap_err();
        assert!(err.contains("not signed by this app's key"), "{err}");
        // The real updater key does not sign the test manifest either.
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let real = conf["plugins"]["updater"]["pubkey"].as_str().unwrap();
        assert!(verified_digests(real, MESSAGE.as_bytes(), SIG).is_err());
        assert!(verified_digests("not base64!", MESSAGE.as_bytes(), SIG).is_err());
        assert!(verified_digests(PUBKEY, MESSAGE.as_bytes(), "").is_err());
    }

    #[test]
    fn a_release_with_a_manifest_is_pulled_by_digest_and_never_by_another_tag() {
        let mut d = verified_digests(PUBKEY, MESSAGE.as_bytes(), SIG).unwrap();
        // The signed fixture predates the launcher image; a real manifest
        // names all five (release.yml refuses to sign one that does not).
        let launcher = format!("sha256:{}", "e".repeat(64));
        d.images.insert("launcher".to_string(), launcher);
        let cmds = pull_commands("v0.9.0-rc23", Some(&d)).unwrap();
        assert_eq!(
            cmds[0],
            vec![
                "pull",
                &format!("ghcr.io/djg3dk/tektonix-agent@sha256:{}", "a".repeat(64))
            ]
        );
        assert_eq!(
            cmds[IMAGES.len()],
            vec![
                "tag",
                &format!("ghcr.io/djg3dk/tektonix-agent@sha256:{}", "a".repeat(64)),
                "tektonix-agent:latest"
            ]
        );
        let err = pull_commands("v0.9.0-rc24", Some(&d)).unwrap_err();
        assert!(err.contains("is for v0.9.0-rc23"), "{err}");
        let short = Digests {
            tag: "v0.9.0-rc23".into(),
            images: BTreeMap::from([("agent".to_string(), "sha256:abc".to_string())]),
        };
        let err = pull_commands("v0.9.0-rc23", Some(&short)).unwrap_err();
        assert!(err.contains("names no digest for router"), "{err}");
    }

    #[test]
    fn start_goes_ahead_on_local_images_when_the_registry_is_away() {
        let warned = start_on_local_images("could not pull x (exit 1)", true).unwrap();
        assert!(warned.starts_with("could not pull x (exit 1). Starting the images already"));
        assert_eq!(
            start_on_local_images("could not pull x (exit 1)", false),
            Err("could not pull x (exit 1)".to_string()),
            "nothing to start"
        );
    }
}
