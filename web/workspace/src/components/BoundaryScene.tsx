import { useEffect, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { COLORS } from "../colors";

/** "The Privacy Boundary" (plan §5.7): one scene only, last.
 * A browser window on the left (user side, lit), a cloud on the
 * right (planner side, dim), a glowing wall between them.
 * Redacted tokens fly through the wall as small dots; raw PII
 * dots bounce off the wall. Procedural, lazy-loaded, pauses
 * during inference (the `paused` prop), static fallback if
 * WebGL fails. It must not block the demo. */

function Wall() {
  return (
    <mesh position={[0, 0, 0]}>
      <planeGeometry args={[0.1, 4, 1]} />
      <meshStandardMaterial color={COLORS.accent} emissive={COLORS.accent} emissiveIntensity={1.2} transparent opacity={0.85} />
    </mesh>
  );
}

function BrowserWindow() {
  return (
    <group position={[-2.2, 0, 0]}>
      <mesh>
        <boxGeometry args={[2.2, 1.6, 0.15]} />
        <meshStandardMaterial color={COLORS.surface} />
      </mesh>
      <mesh position={[0, 0, 0.1]}>
        <planeGeometry args={[1.9, 1.3]} />
        <meshStandardMaterial color={COLORS.bgRaised} emissive={COLORS.info} emissiveIntensity={0.35} />
      </mesh>
    </group>
  );
}

function Cloud() {
  return (
    <group position={[2.2, 0.3, 0]}>
      {[
        [0, 0, 0, 0.9],
        [0.8, 0.2, 0, 0.7],
        [-0.8, 0.15, 0, 0.65],
        [0.3, 0.6, 0, 0.6],
      ].map(([x, y, z, r], i) => (
        <mesh key={i} position={[x, y, z]}>
          <sphereGeometry args={[r, 24, 24]} />
          <meshStandardMaterial color={COLORS.border} transparent opacity={0.75} />
        </mesh>
      ))}
    </group>
  );
}

type DotProps = { start: [number, number, number]; raw: boolean; speed: number; paused: boolean };

function Dot({ start, raw, speed, paused }: DotProps) {
  const ref = useRef<THREE.Mesh>(null);
  const x = useRef(start[0]);
  const dir = useRef(1);
  useEffect(() => {
    x.current = start[0];
    dir.current = 1;
  }, [start]);
  useFrame((_, delta) => {
    if (paused || !ref.current) return;
    x.current += dir.current * speed * delta;
    if (raw && x.current > -0.15) {
      // Raw PII bounces off the wall.
      x.current = -0.15;
      dir.current = -1;
    }
    if (!raw && x.current > 3.5) x.current = start[0];
    if (raw && x.current <= start[0]) {
      x.current = start[0];
      dir.current = 1;
    }
    ref.current.position.x = x.current;
  });
  return (
    <mesh ref={ref} position={start}>
      <sphereGeometry args={[0.06, 12, 12]} />
      <meshStandardMaterial
        color={raw ? COLORS.danger : COLORS.success}
        emissive={raw ? COLORS.danger : COLORS.success}
        emissiveIntensity={1.5}
      />
    </mesh>
  );
}

function Scene({ paused }: { paused: boolean }) {
  return (
    <>
      <ambientLight intensity={0.5} />
      <pointLight position={[-3, 2, 3]} intensity={30} color={COLORS.text} />
      <pointLight position={[3, 1, 2]} intensity={8} color={COLORS.info} />
      <BrowserWindow />
      <Cloud />
      <Wall />
      <Dot start={[-2.2, 0.5, 0.2]} raw={false} speed={0.9} paused={paused} />
      <Dot start={[-2.2, -0.3, 0.2]} raw={false} speed={0.7} paused={paused} />
      <Dot start={[-2.2, 0.1, 0.2]} raw={true} speed={0.8} paused={paused} />
    </>
  );
}

function webglAvailable(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return !!(
      canvas.getContext("webgl2") ||
      canvas.getContext("webgl") ||
      canvas.getContext("experimental-webgl")
    );
  } catch {
    return false;
  }
}

export function BoundaryScene({ paused = false }: { paused?: boolean }) {
  const [gl, setGl] = useState<boolean | null>(null);
  const [reducedMotion, setReducedMotion] = useState(false);
  useEffect(() => {
    setGl(webglAvailable());
    // Sprint 9 fix (review M17): the useFrame loop ignored
    // prefers-reduced-motion. Freeze the dots when it is set.
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReducedMotion(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  const frozen = paused || reducedMotion;
  if (gl === false) {
    return (
      <div className="scene-fallback" role="img" aria-label="Privacy Boundary: your browser on the left, a glowing wall in the middle, the cloud planner on the right. Redacted tokens pass through the wall; raw personal data bounces off.">
        <p><strong>The Privacy Boundary</strong></p>
        <p className="secondary">
          Your browser (left) · glowing wall (middle) · cloud planner (right).
          Redacted tokens pass through; raw personal data bounces off.
        </p>
        <p className="muted">WebGL is unavailable, so this is the static fallback.</p>
      </div>
    );
  }
  if (gl === null) return <p className="muted">Loading the scene…</p>;
  return (
    <div className="scene" role="img" aria-label="Privacy Boundary 3D scene">
      <Canvas camera={{ position: [0, 0, 6], fov: 45 }} dpr={[1, 2]}>
        <Scene paused={frozen} />
      </Canvas>
    </div>
  );
}
