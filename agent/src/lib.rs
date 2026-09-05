//! The spark-pulse node agent.
//!
//! Split into a library and a thin binary so the integration tests can drive
//! the same code the binary runs, rather than a copy of it.

pub mod enroll;
pub mod executor;
pub mod facts;
pub mod identity;
pub mod proto;
pub mod session;
