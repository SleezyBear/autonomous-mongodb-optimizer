export default function Mark() {
  // A sorted index: keys in ascending order beneath a root.
  return (
    <svg viewBox="0 0 28 28" className="h-7 w-7" aria-hidden>
      <rect x="1" y="1" width="26" height="26" rx="6" fill="var(--ink-3)" />
      <path d="M14 7v5M8 12h12M8 12v4M14 12v4M20 12v4" stroke="var(--faint)" strokeWidth="1.4" fill="none" />
      <rect x="5.5" y="16" width="5" height="5" rx="1" fill="var(--accent)" />
      <rect x="11.5" y="16" width="5" height="5" rx="1" fill="var(--accent)" opacity=".75" />
      <rect x="17.5" y="16" width="5" height="5" rx="1" fill="var(--accent)" opacity=".5" />
    </svg>
  );
}
