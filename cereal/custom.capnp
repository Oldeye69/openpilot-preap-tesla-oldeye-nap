using Cxx = import "./include/c++.capnp";
$Cxx.namespace("cereal");

@0xb526ba661d550a59;

# custom.capnp: a home for empty structs reserved for custom forks
# These structs are guaranteed to remain reserved and empty in mainline
# cereal, so use these if you want custom events in your fork.

# DO rename the structs
# DON'T change the identifier (e.g. @0x81c2f05a394cf4af)

struct RoadContext @0x81c2f05a394cf4af {
  timestamp @0 :Float64;
  speed @1 :SpeedLimit;
  enforcement @2 :List(Enforcement);
  lighting @3 :Lighting;
  country @4 :Text;
  policyState @5 :Text;
  policyPrompt @6 :Text;
  warningsEnabled @7 :Bool;
  disabledReason @8 :Text;
  diagnostics @9 :Text;
  schemaVersion @10 :UInt16;
  struct SpeedLimit {
    valid @0 :Bool;
    effectiveLimit @1 :Float32;
    source @2 :Text;
    confidence @3 :Float32;
    age @4 :Float32;
    limitType @5 :Text;
    roadId @6 :Text;
    roadMatchConfidence @7 :Float32;
    directionMatch @8 :Bool;
    cameraLimit @9 :Float32;
    mapLimit @10 :Float32;
    nextLimit @11 :Float32;
    distanceToNextLimit @12 :Float32;
    hasCamera @13 :Bool;
    hasMap @14 :Bool;
    hasNext @15 :Bool;
    conflict @16 :Bool;
    controlEligible @17 :Bool;
    reason @18 :Text;
    unit @19 :Text;
  }
  struct Enforcement {
    kind @0 :Text;
    distance @1 :Float32;
    limit @2 :Float32;
    hasLimit @3 :Bool;
    source @4 :Text;
    identity @5 :Text;
    eta @6 :Float32;
    hasEta @7 :Bool;
    remainingDistance @8 :Float32;
    hasRemaining @9 :Bool;
  }
  struct Lighting {
    highBeamAllowed @0 :Bool;
    confidence @1 :Float32;
    reason @2 :Text;
    age @3 :Float32;
    timestamp @4 :Float64;
    state @5 :Text;
    requestedLightState @6 :Text;
    ambient @7 :Text;
  }
}

struct CustomReserved1 @0xaedffd8f31e7b55d {
}

struct CustomReserved2 @0xf35cc4560bbf6ec2 {
}

struct CustomReserved3 @0xda96579883444c35 {
}

struct CustomReserved4 @0x80ae746ee2596b11 {
}

struct CustomReserved5 @0xa5cd762cd951a455 {
}

struct CustomReserved6 @0xf98d843bfd7004a3 {
}

struct CustomReserved7 @0xb86e6369214c01c8 {
}

struct CustomReserved8 @0xf416ec09499d9d19 {
}

struct CustomReserved9 @0xa1680744031fdb2d {
}

struct CustomReserved10 @0xcb9fd56c7057593a {
}

struct CustomReserved11 @0xc2243c65e0340384 {
}

struct CustomReserved12 @0x9ccdc8676701b412 {
}

struct CustomReserved13 @0xcd96dafb67a082d0 {
}

struct CustomReserved14 @0xb057204d7deadf3f {
}

struct CustomReserved15 @0xbd443b539493bc68 {
}

struct CustomReserved16 @0xfc6241ed8877b611 {
}

struct CustomReserved17 @0xa30662f84033036c {
}

struct CustomReserved18 @0xc86a3d38d13eb3ef {
}

struct CustomReserved19 @0xa4f1eb3323f5f582 {
}
