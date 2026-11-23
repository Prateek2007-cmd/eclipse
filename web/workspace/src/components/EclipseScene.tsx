import { useEffect, useRef } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import * as THREE from "three";

/** EclipseScene (PR #6, Worker 9): the hero 3D moment — an
 * eclipse in miniature. One pale disc (the sun/moon), a
 * thin tilted ring around it, a faint halo. Procedural
 * geometry only — no model files. Same stack as
 * BoundaryScene: plain three + @react-three/fiber, no new
 * dependency.
 *
 * Calm and cheap: 160px canvas, dpr capped at 1.5, one
 * revolution per 60s, frameloop="demand" so the render loop
 * only runs while the ring is turning — and it stops
 * entirely when the tab is hidden (VisibilityGate restarts
 * it on return). Only mounted when EclipseHero has already
 * ruled out reduced motion, narrow screens, and missing
 * WebGL. */

const ANGULAR_SPEED = (Math.PI * 2) / 60; // one revolution per 60s

type PausedRef = { current: boolean };

function EclipseGroup({ pausedRef }: { pausedRef: PausedRef }) {
  const group = useRef<THREE.Group>(null);
  useFrame((state, delta) => {
    // Paused: return without invalidate() and the demand
    // loop stops — no frames render while hidden.
    if (pausedRef.current) return;
    if (group.current) group.current.rotation.y += ANGULAR_SPEED * delta;
    state.invalidate();
  });
  return (
    <group ref={group} rotation={[0.12, 0, -0.08]}>
      {/* The disc: warm pale, matte, day-eclipse light. */}
      <mesh>
        <sphereGeometry args={[1, 48, 48]} />
        <meshStandardMaterial color="#F8F1E4" roughness={0.9} metalness={0} />
      </mesh>
      {/* The ring: thin, tilted, pale steel blue. */}
      <mesh rotation={[Math.PI / 2.4, 0.15, 0]}>
        <torusGeometry args={[1.45, 0.05, 16, 96]} />
        <meshStandardMaterial color="#A9C6E2" roughness={0.35} metalness={0.1} />
      </mesh>
      {/* The halo: a faint outer ring, barely there. */}
      <mesh rotation={[Math.PI / 2.4, 0.15, 0]}>
        <torusGeometry args={[1.75, 0.015, 8, 96]} />
        <meshBasicMaterial color="#DCE9F5" transparent opacity={0.8} />
      </mesh>
    </group>
  );
}

/** Pauses the demand loop while the tab is hidden and kicks
 * it again on return. Lives inside the Canvas so it can
 * call invalidate(). */
function VisibilityGate({ pausedRef }: { pausedRef: PausedRef }) {
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    const onVis = () => {
      pausedRef.current = document.hidden;
      if (!document.hidden) invalidate();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [invalidate, pausedRef]);
  return null;
}

export default function EclipseScene() {
  const pausedRef = useRef(false);
  return (
    <Canvas
      frameloop="demand"
      dpr={[1, 1.5]}
      camera={{ position: [0, 0, 5.2], fov: 38 }}
      gl={{ alpha: true, antialias: true, powerPreference: "low-power" }}
      onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
    >
      <ambientLight intensity={1.1} />
      <directionalLight position={[3, 4, 5]} intensity={1.2} color="#FFF6E8" />
      <directionalLight position={[-4, -2, 3]} intensity={0.4} color="#DCE9F5" />
      <EclipseGroup pausedRef={pausedRef} />
      <VisibilityGate pausedRef={pausedRef} />
    </Canvas>
  );
}
