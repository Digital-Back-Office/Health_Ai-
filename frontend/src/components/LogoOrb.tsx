/** The Health AI mark: a small version of the assistant's AI orb. */
export function LogoOrb({ size = 42 }: { size?: number }) {
  return (
    <span className="logo-orb" style={{ width: size, height: size }} role="img" aria-label="Health AI">
      <span className="logo-orb__swirl" />
      <span className="logo-orb__gloss" />
    </span>
  );
}
