//! Just enough XML to extract the OME-XML items of §3.2.

use crate::{Error, reject};

#[derive(Debug)]
pub enum Event {
    Start { local: String, attrs: Vec<(String, String)>, empty: bool },
    End,
}

fn local_name(qname: &str) -> &str {
    match qname.rfind(':') {
        Some(i) => &qname[i + 1..],
        None => qname,
    }
}

/// Decode the five predefined entities and numeric character references.
/// Any other `&...;` sequence is kept as written.
pub fn decode_entities(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    let mut rest = raw;
    while let Some(i) = rest.find('&') {
        out.push_str(&rest[..i]);
        rest = &rest[i..];
        let Some(j) = rest.find(';') else { break };
        let ent = &rest[1..j];
        let rep = match ent {
            "lt" => Some('<'),
            "gt" => Some('>'),
            "amp" => Some('&'),
            "quot" => Some('"'),
            "apos" => Some('\''),
            _ => {
                if let Some(h) = ent.strip_prefix("#x").or_else(|| ent.strip_prefix("#X")) {
                    u32::from_str_radix(h, 16).ok().and_then(char::from_u32)
                } else if let Some(d) = ent.strip_prefix('#') {
                    d.parse::<u32>().ok().and_then(char::from_u32)
                } else {
                    None
                }
            }
        };
        match rep {
            Some(c) => {
                out.push(c);
                rest = &rest[j + 1..];
            }
            None => {
                out.push('&');
                rest = &rest[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

pub fn parse(doc: &str) -> Result<Vec<Event>, Error> {
    let b = doc.as_bytes();
    let mut i = 0;
    let mut ev = Vec::new();
    let find = |from: usize, pat: &str| doc[from..].find(pat).map(|k| from + k);
    while i < b.len() {
        if b[i] != b'<' {
            i += 1;
            continue;
        }
        let r = &doc[i..];
        if r.starts_with("<!--") {
            i = find(i + 4, "-->").ok_or(Error::Reject("unterminated XML comment".into()))? + 3;
        } else if r.starts_with("<![CDATA[") {
            i = find(i + 9, "]]>").ok_or(Error::Reject("unterminated CDATA".into()))? + 3;
        } else if r.starts_with("<?") {
            i = find(i + 2, "?>").ok_or(Error::Reject("unterminated XML PI".into()))? + 2;
        } else if r.starts_with("<!") {
            // DOCTYPE etc.: skip to the matching '>' (allowing an internal subset)
            let mut depth = 0i32;
            let mut j = i + 2;
            while j < b.len() {
                match b[j] {
                    b'[' => depth += 1,
                    b']' => depth -= 1,
                    b'>' if depth <= 0 => break,
                    _ => {}
                }
                j += 1;
            }
            i = j + 1;
        } else if r.starts_with("</") {
            i = find(i, ">").ok_or(Error::Reject("unterminated end tag".into()))? + 1;
            ev.push(Event::End);
        } else {
            // start tag: scan honoring quoted attribute values
            let mut j = i + 1;
            let mut quote = 0u8;
            while j < b.len() {
                let c = b[j];
                if quote != 0 {
                    if c == quote {
                        quote = 0;
                    }
                } else if c == b'"' || c == b'\'' {
                    quote = c;
                } else if c == b'>' {
                    break;
                }
                j += 1;
            }
            if j >= b.len() {
                return reject("unterminated start tag in OME-XML");
            }
            let mut inner = &doc[i + 1..j];
            let empty = inner.ends_with('/');
            if empty {
                inner = &inner[..inner.len() - 1];
            }
            let name_end = inner
                .find(|c: char| c.is_ascii_whitespace())
                .unwrap_or(inner.len());
            let qname = &inner[..name_end];
            let mut attrs = Vec::new();
            let mut a = &inner[name_end..];
            loop {
                a = a.trim_start();
                if a.is_empty() {
                    break;
                }
                let Some(eq) = a.find('=') else {
                    return reject("malformed attribute in OME-XML");
                };
                let an = a[..eq].trim().to_string();
                let rest = a[eq + 1..].trim_start();
                let q = rest.chars().next().unwrap_or(' ');
                if q != '"' && q != '\'' {
                    return reject("unquoted attribute in OME-XML");
                }
                let Some(close) = rest[1..].find(q) else {
                    return reject("unterminated attribute in OME-XML");
                };
                attrs.push((an, decode_entities(&rest[1..1 + close])));
                a = &rest[close + 2..];
            }
            ev.push(Event::Start { local: local_name(qname).to_string(), attrs, empty });
            i = j + 1;
        }
    }
    Ok(ev)
}

pub fn attr<'a>(attrs: &'a [(String, String)], name: &str) -> Option<&'a str> {
    // attribute names are matched unprefixed, as written
    attrs.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
}

#[derive(Debug, Default)]
pub struct TiffData {
    pub attrs: Vec<(String, String)>,
    pub uuid_filename: Option<String>,
}

#[derive(Debug, Default)]
pub struct Ome {
    pub image_name: Option<String>,
    pub pixels: Vec<(String, String)>,
    pub tiffdata: Vec<TiffData>,
}

pub fn extract(doc: &str) -> Result<Ome, Error> {
    let ev = parse(doc)?;
    let mut ome = Ome::default();
    let mut seen_image = false;
    let mut seen_pixels = false;
    // depth tracking
    let mut depth = 0usize;
    let mut pixels_depth: Option<usize> = None; // depth of the open first Pixels
    let mut tiffdata_depth: Option<usize> = None;
    for e in ev {
        match e {
            Event::Start { local, attrs, empty } => {
                if local == "Image" && !seen_image {
                    seen_image = true;
                    ome.image_name = attr(&attrs, "Name").map(|s| s.to_string());
                }
                let mut opened_pixels = false;
                let mut opened_td = false;
                if local == "Pixels" && !seen_pixels {
                    seen_pixels = true;
                    ome.pixels = attrs.clone();
                    opened_pixels = true;
                } else if pixels_depth.is_some() && local == "TiffData" {
                    ome.tiffdata.push(TiffData { attrs: attrs.clone(), uuid_filename: None });
                    opened_td = true;
                } else if tiffdata_depth.is_some() && local == "UUID" {
                    if let Some(f) = attr(&attrs, "FileName") {
                        let td = ome.tiffdata.last_mut().unwrap();
                        if td.uuid_filename.is_none() {
                            td.uuid_filename = Some(f.to_string());
                        }
                    }
                }
                if !empty {
                    depth += 1;
                    if opened_pixels {
                        pixels_depth = Some(depth);
                    }
                    if opened_td {
                        tiffdata_depth = Some(depth);
                    }
                }
            }
            Event::End => {
                if tiffdata_depth == Some(depth) {
                    tiffdata_depth = None;
                }
                if pixels_depth == Some(depth) {
                    pixels_depth = None;
                }
                depth = depth.saturating_sub(1);
            }
        }
    }
    Ok(ome)
}
