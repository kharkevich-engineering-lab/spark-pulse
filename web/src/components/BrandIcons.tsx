/** The brand marks, as components rather than files.
 *
 * Inline because both are drawn in one colour and have to follow the theme:
 * the sources hardcode `#000000`, which is invisible on the dark palette. As
 * components they take `currentColor` and inherit whatever the surrounding
 * text is, the same way every lucide icon on these pages already does.
 *
 * The logo is the exception — it is a full multi-colour mark and lives in
 * `assets/kharkevich-logo.svg`, imported as a URL.
 */

/** The product mark: a pulse trace. Replaces the generic lightning bolt. */
export function PulseIcon({ size = 24, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 512 512"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={32}
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-hidden="true"
    >
      <polyline points="48 320 112 320 176 64 240 448 304 224 336 320 400 320" />
      <circle cx="432" cy="320" r="32" />
    </svg>
  );
}

/** Half sun, half moon: the theme that follows the operating system.
 *
 * The two explicit choices keep lucide's `Sun` and `Moon`; this is the third
 * state, and it needs to read as "both" rather than as a third unrelated
 * symbol.
 */
export function SunMoonIcon({ size = 18, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-hidden="true"
    >
      <line x1="16" y1="3" x2="16" y2="29" />
      <path d="M16,23c-3.87,0-7-3.13-7-7s3.13-7,7-7" />
      <line x1="6.81" y1="6.81" x2="8.93" y2="8.93" />
      <line x1="3" y1="16" x2="6" y2="16" />
      <line x1="6.81" y1="25.19" x2="8.93" y2="23.07" />
      <path d="M16,12.55C17.2,10.43,19.48,9,22.09,9c0.16,0,0.31,0.01,0.47,0.02c-1.67,0.88-2.8,2.63-2.8,4.64c0,2.9,2.35,5.25,5.25,5.25c1.6,0,3.03-0.72,3.99-1.85C28.48,20.43,25.59,23,22.09,23c-2.61,0-4.89-1.43-6.09-3.55" />
    </svg>
  );
}
