//! Error classes (spec §8.4).

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorClass {
    Archive,
    Entry,
    Body,
    Payload,
    Resolution,
    Request,
}

impl ErrorClass {
    pub fn as_str(&self) -> &'static str {
        match self {
            ErrorClass::Archive => "archive",
            ErrorClass::Entry => "entry",
            ErrorClass::Body => "body",
            ErrorClass::Payload => "payload",
            ErrorClass::Resolution => "resolution",
            ErrorClass::Request => "request",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VzError {
    pub class: ErrorClass,
    pub message: String,
}

impl VzError {
    pub fn new(class: ErrorClass, message: impl Into<String>) -> Self {
        VzError { class, message: message.into() }
    }
    pub fn archive(m: impl Into<String>) -> Self {
        Self::new(ErrorClass::Archive, m)
    }
    pub fn entry(m: impl Into<String>) -> Self {
        Self::new(ErrorClass::Entry, m)
    }
    pub fn body(m: impl Into<String>) -> Self {
        Self::new(ErrorClass::Body, m)
    }
    pub fn payload(m: impl Into<String>) -> Self {
        Self::new(ErrorClass::Payload, m)
    }
    pub fn resolution(m: impl Into<String>) -> Self {
        Self::new(ErrorClass::Resolution, m)
    }
    pub fn request(m: impl Into<String>) -> Self {
        Self::new(ErrorClass::Request, m)
    }
}

impl fmt::Display for VzError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} error: {}", self.class.as_str(), self.message)
    }
}

impl std::error::Error for VzError {}

pub type Result<T> = std::result::Result<T, VzError>;
