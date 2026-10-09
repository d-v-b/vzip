//! The transforms of derived elements (conventions §8.5): every one a parser emits, by
//! name, and whether a validator can derive its bytes again from the source (the view
//! check, conventions §8.8). A derived transform may hold shown values only if it can.

use serde_json::Value as J;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Transform {
    /// ND2: a compressed LV record (a 12-byte header, then a zlib stream)
    Nd2LvZlib,
    /// ND2: an XML variant chunk (its bytes, as UTF-8 text)
    XmlVariant,
    /// CZI: a compressed attachment (`Zip-Comp`, `ZIP`), whose space holds nothing
    Gzip,
}

impl Transform {
    /// Every transform; `index` keeps it complete (each variant at its index).
    pub const ALL: [Transform; 3] = [Transform::Nd2LvZlib, Transform::XmlVariant, Transform::Gzip];

    pub fn index(self) -> usize {
        match self {
            Transform::Nd2LvZlib => 0,
            Transform::XmlVariant => 1,
            Transform::Gzip => 2,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Transform::Nd2LvZlib => "nd2-lv-zlib",
            Transform::XmlVariant => "xml-variant",
            Transform::Gzip => "gzip",
        }
    }

    /// The transform a form names, if one of these.
    pub fn of_form(form: &str) -> Option<Transform> {
        let v: J = serde_json::from_str(form).ok()?;
        let t = v["transform"].as_str()?;
        Transform::ALL.into_iter().find(|x| x.name() == t)
    }

    /// Whether its space may hold values the view shows (conventions §8.8: only when a
    /// validator can derive its bytes again).
    pub fn may_hold_shown(self) -> bool {
        match self {
            Transform::Nd2LvZlib | Transform::XmlVariant => true,
            Transform::Gzip => false,
        }
    }

    /// The derived bytes from the element's source bytes `raw` and its form, or None
    /// when a validator cannot derive them (the transform may then hold no shown value).
    pub fn rederive(self, raw: &[u8], form: &J) -> Option<Result<Vec<u8>, String>> {
        match self {
            Transform::Nd2LvZlib if raw.len() < 12 => Some(Err("a compressed LV record of fewer than 12 bytes".into())),
            Transform::Nd2LvZlib => Some(crate::lv::inflate(raw, form["size"].as_u64())),
            Transform::XmlVariant => Some(Ok(raw.to_vec())),
            Transform::Gzip => None,
        }
    }
}
