//! The expression language and the role checker of the TIFF and CZI schemas: one
//! test over valid combinations, and one test per error.

use std::collections::HashMap;
use vzip_ir::expr::{self, Env, V};
use vzip_ir::rules::{self, Rules};

fn ev(s: &str, env: &Env) -> V {
    expr::eval(&expr::parse(s).unwrap(), env)
}

#[test]
fn expressions_evaluate() {
    let mut env: Env = HashMap::new();
    env.insert("bits".into(), V::List(vec![V::Num(8.0), V::Num(8.0), V::Num(8.0)]));
    env.insert("mixed".into(), V::List(vec![V::Num(8.0), V::Num(16.0)]));
    env.insert("name".into(), V::Str("II".into()));
    env.insert("table".into(), V::from_json(&serde_json::json!({"7": {"kind": "a"}, "x": [1, 2]})));
    let cases: [(&str, V); 18] = [
        ("in(3, [1, 3, 4])", V::Bool(true)),
        ("in(2, [1, 3, 4])", V::Bool(false)),
        ("in(name, ['II', 'MM'])", V::Bool(true)),
        ("name == 'II'", V::Bool(true)),
        ("name != 'MM'", V::Bool(true)),
        ("missing == 'II'", V::Bool(false)),
        ("missing != 'II'", V::Bool(false)),
        ("distinct(bits)", V::Num(1.0)),
        ("distinct(mixed)", V::Num(2.0)),
        ("first(mixed)", V::Num(8.0)),
        ("first(missing)", V::Absent),
        ("at(table, 7).kind", V::Absent), // a path after a call is not part of the grammar's primaries
        ("min(mixed) + max(3, 9)", V::Num(17.0)),
        ("present(missing) || present(name)", V::Bool(true)),
        ("[1, 1] == [1, 1]", V::Bool(true)),
        ("true && !false", V::Bool(true)),
        ("len(at(table, 'x'))", V::Num(2.0)),
        ("missing ?? 4", V::Num(4.0)),
    ];
    for (s, want) in cases {
        if s.starts_with("at(table, 7).") {
            assert!(expr::parse(s).is_err(), "{s}");
            continue;
        }
        assert_eq!(ev(s, &env), want, "{s}");
    }
}

#[test]
fn an_unterminated_string_is_an_error() {
    assert!(expr::parse("name == 'II").is_err());
}

#[test]
fn an_unclosed_list_is_an_error() {
    assert!(expr::parse("in(1, [1, 2)").is_err());
}

#[test]
fn trailing_tokens_are_an_error() {
    assert!(expr::parse("1 2").is_err());
}

const SCHEMA: &str = r#"{
  "limits": {"max": 10},
  "tables": {"kinds": {"a": [1, 2]}},
  "roles": {
    "thing": {
      "derive": {"allowed": "at(kinds, kind)", "twice": "n * 2"},
      "constraints": [
        {"require": "in(type, allowed)", "message": "type {type} is not {kind}"},
        {"require": "twice <= max", "message": "{n} is more than half of {max}"}
      ]
    }
  }
}"#;

#[test]
fn roles_derive_then_check_in_order() {
    let r = Rules::new(SCHEMA).unwrap();
    let cases = [
        (1.0, 5.0, Ok(())),
        (3.0, 5.0, Err("type 3 is not a".to_string())),
        (2.0, 6.0, Err("6 is more than half of 10".to_string())),
        (3.0, 6.0, Err("type 3 is not a".to_string())), // the first constraint that fails
    ];
    for (t, n, want) in cases {
        let mut env = rules::env([("type", rules::num(t)), ("n", rules::num(n)), ("kind", rules::text("a"))]);
        assert_eq!(r.check("thing", &mut env), want);
    }
    assert_eq!(r.limit("max"), 10.0);
}

#[test]
fn an_unknown_role_is_an_error() {
    let r = Rules::new(SCHEMA).unwrap();
    assert!(r.check("other", &mut rules::Vars::default()).is_err());
}

#[test]
fn a_constraint_without_a_message_is_an_error() {
    assert!(Rules::new(r#"{"roles": {"x": {"constraints": [{"require": "true"}]}}}"#).is_err());
}

#[test]
fn a_bad_expression_is_an_error() {
    assert!(Rules::new(r#"{"roles": {"x": {"constraints": [{"require": "1 +", "message": "m"}]}}}"#).is_err());
}

#[test]
fn the_tiff_and_czi_schemas_are_valid() {
    let (t, c) = (rules::tiff(), rules::czi());
    assert!(t.has_role("field") && t.has_role("tile") && c.has_role("entry") && c.has_role("extent"));
}
