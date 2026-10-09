//! The bytes a sans-IO parser was fed, held until the step that uses them.

use std::collections::BTreeMap;

#[derive(Default)]
pub struct Fed {
    got: BTreeMap<u64, Vec<u8>>,
    held: u64,
}

impl Fed {
    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        match self.got.get(&offset) {
            Some(b) if b.len() >= data.len() => {}
            _ => {
                self.held += data.len() as u64;
                if let Some(old) = self.got.insert(offset, data) {
                    self.held -= old.len() as u64;
                }
            }
        }
    }

    /// The bytes [o, o + n), when one fed range holds them.
    pub fn get(&self, o: u64, n: u64) -> Option<&[u8]> {
        let end = o.checked_add(n)?;
        for (&s, b) in self.got.range(..=o).rev() {
            if end <= s + b.len() as u64 {
                return Some(&b[(o - s) as usize..(end - s) as usize]);
            }
            if s + (1 << 26) < o {
                break; // no fed range is that long
            }
        }
        None
    }

    pub fn clear(&mut self) {
        self.got.clear();
        self.held = 0;
    }

    /// The bytes held.
    pub fn held(&self) -> u64 {
        self.held
    }
}

pub fn u16_at(b: &[u8], at: usize, le: bool) -> u16 {
    let a: [u8; 2] = b[at..at + 2].try_into().unwrap();
    if le { u16::from_le_bytes(a) } else { u16::from_be_bytes(a) }
}

pub fn u32_at(b: &[u8], at: usize, le: bool) -> u32 {
    let a: [u8; 4] = b[at..at + 4].try_into().unwrap();
    if le { u32::from_le_bytes(a) } else { u32::from_be_bytes(a) }
}

pub fn u64_at(b: &[u8], at: usize, le: bool) -> u64 {
    let a: [u8; 8] = b[at..at + 8].try_into().unwrap();
    if le { u64::from_le_bytes(a) } else { u64::from_be_bytes(a) }
}

pub fn i32le(b: &[u8], at: usize) -> i32 {
    i32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}

pub fn i64le(b: &[u8], at: usize) -> i64 {
    i64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}
