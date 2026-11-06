/**
 * Full-screen 3D background.
 * `phase` comes from AppShell and moves the camera:
 *   landing → far view
 *   auth    → closer to the core
 *   workspace → top-down over the deck
 */
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Float, Stars } from "@react-three/drei";
import { useRef } from "react";
import * as THREE from "three";

export type ScenePhase = "landing" | "auth" | "workspace";

// Where the camera should sit for each screen
const CAMERA = {
  landing: { x: 0, y: 2.4, z: 7.5 },
  auth: { x: 2.8, y: 1.6, z: 5.2 },
  workspace: { x: 0.2, y: 4.2, z: 5.8 },
};

function CameraMover({ phase }: { phase: ScenePhase }) {
  const { camera } = useThree();
  const lookAt = useRef(new THREE.Vector3(0, 0.2, 0));

  useFrame(() => {
    const goal = CAMERA[phase];
    // Smoothly move toward the goal each frame (simple lerp)
    camera.position.x += (goal.x - camera.position.x) * 0.05;
    camera.position.y += (goal.y - camera.position.y) * 0.05;
    camera.position.z += (goal.z - camera.position.z) * 0.05;

    const lookY = phase === "workspace" ? -0.2 : 0.2;
    lookAt.current.y += (lookY - lookAt.current.y) * 0.05;
    camera.lookAt(lookAt.current);
  });

  return null;
}

/** Small shape that orbits around the center. */
function OrbitNode({
  radius,
  speed,
  size,
  color,
  bright,
}: {
  radius: number;
  speed: number;
  size: number;
  color: string;
  bright: boolean;
}) {
  const mesh = useRef<THREE.Mesh>(null);
  const angle = useRef(Math.random() * Math.PI * 2);

  useFrame((_, delta) => {
    if (!mesh.current) return;
    angle.current += delta * speed;
    mesh.current.position.x = Math.cos(angle.current) * radius;
    mesh.current.position.z = Math.sin(angle.current) * radius;
    mesh.current.position.y = Math.sin(angle.current * 2) * 0.1;
  });

  return (
    <mesh ref={mesh}>
      <octahedronGeometry args={[size, 0]} />
      <meshStandardMaterial
        color={color}
        emissive={color}
        emissiveIntensity={bright ? 0.8 : 0.25}
        metalness={0.7}
        roughness={0.25}
      />
    </mesh>
  );
}

/** Center “query core” + floor + orbiting nodes. */
function CommandDeck({ phase }: { phase: ScenePhase }) {
  const core = useRef<THREE.Mesh>(null);
  const onAuth = phase !== "landing";
  const inApp = phase === "workspace";

  useFrame(({ clock }) => {
    if (!core.current) return;
    const t = clock.getElapsedTime();
    core.current.rotation.y = t * 0.35;
    // Pulse a bit more after login
    const scale = inApp ? 1.15 : onAuth ? 1.05 : 0.95;
    core.current.scale.setScalar(scale);
  });

  return (
    <group>
      {/* Floor disc */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -1.35, 0]}>
        <circleGeometry args={[4.2, 64]} />
        <meshStandardMaterial
          color="#0f172a"
          emissive="#0ea5e9"
          emissiveIntensity={inApp ? 0.3 : 0.08}
          metalness={0.85}
          roughness={0.35}
          transparent
          opacity={inApp ? 0.55 : 0.22}
        />
      </mesh>

      {/* Glowing core */}
      <mesh ref={core} position={[0, 0.15, 0]}>
        <icosahedronGeometry args={[0.55, 1]} />
        <meshStandardMaterial
          color="#e2e8f0"
          emissive={inApp ? "#38bdf8" : "#64748b"}
          emissiveIntensity={inApp ? 1.1 : onAuth ? 0.55 : 0.2}
          metalness={0.9}
          roughness={0.15}
        />
      </mesh>

      {/* Ring around the core */}
      <mesh position={[0, 0.15, 0]} rotation={[Math.PI / 2.4, 0, 0]}>
        <torusGeometry args={[1.15, 0.025, 16, 80]} />
        <meshStandardMaterial
          color="#67e8f9"
          emissive="#22d3ee"
          emissiveIntensity={onAuth ? 0.7 : 0.2}
          metalness={0.6}
          roughness={0.2}
        />
      </mesh>

      <OrbitNode radius={2.1} speed={0.35} size={0.16} color="#38bdf8" bright={onAuth} />
      <OrbitNode radius={2.6} speed={-0.22} size={0.13} color="#94a3b8" bright={onAuth} />
      <OrbitNode radius={3.1} speed={0.18} size={0.18} color="#5eead4" bright={inApp} />

      <pointLight
        position={[0, 1.5, 0]}
        intensity={inApp ? 2.2 : onAuth ? 1.4 : 0.7}
        color="#7dd3fc"
        distance={12}
      />
    </group>
  );
}

export function Scene({ phase }: { phase: ScenePhase }) {
  return (
    <div className="scene-canvas" aria-hidden>
      <Canvas
        dpr={[1, 1.75]}
        camera={{ position: [0, 2.4, 7.5], fov: 42 }}
      >
        <color attach="background" args={["#020617"]} />
        <ambientLight intensity={0.25} />
        <directionalLight position={[4, 6, 2]} intensity={1} color="#e2e8f0" />
        <directionalLight position={[-5, 2, -3]} intensity={0.4} color="#38bdf8" />
        <fog attach="fog" args={["#020617", 8, 22]} />

        <CameraMover phase={phase} />
        <Float speed={1.1} rotationIntensity={0.15} floatIntensity={0.35}>
          <CommandDeck phase={phase} />
        </Float>
        <Stars radius={40} depth={30} count={1600} factor={3} saturation={0} fade speed={0.4} />
      </Canvas>
      <div className="scene-vignette" />
    </div>
  );
}
