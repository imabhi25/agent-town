// Small interface glyphs. The adjacent text names the controls.
const paths = {
  town: "M3 21h18M5 21V9l8-5v17m0-10h6v10M8 11v1m2-2v1m-2 4v1m2-2v1m6-1v2",
  chat: "M4 4h16v12H9l-5 4V4Zm4 5h8m-8 3h5",
  sparkle: "M12 2l2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5L12 2Z",
  graph: "M8 12h4m0 0v-6h4m-4 6v6h4M4 10h4v4H4zm12-6h4v4h-4zm0 12h4v4h-4z",
  workers: "M15 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2m18 0v-2a4 4 0 0 0-3-3.9M12.5 8.5a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0ZM16 5a3.5 3.5 0 0 1 0 7",
  map: "m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Zm6-3v15m6-12v15",
  arrow: "M4 12h15m-6-6 6 6-6 6",
  external: "M7 17 17 7M7 7h10v10",
  chevron: "m9 6 6 6-6 6",
  play: "m7 4 14 8-14 8V4Z",
  pause: "M6 5h4v14H6zm8 0h4v14h-4z",
  restart: "M4 10a8 8 0 1 1 1.5 7M4 4v6h6",
  sliders: "M3 6h7m4 0h7M3 12h12m4 0h2M3 18h3m4 0h11M10 3v6m5 0v6M6 15v6",
  bolt: "m13 2-9 12h7l-1 8 10-12h-7l1-8Z",
  tray: "M4 4h16v16H4zm0 10h5l2 3h2l2-3h5",
  github: "M12 1.9a10.1 10.1 0 0 0-3.2 19.7c.5.1.7-.2.7-.5v-1.9c-2.8.6-3.4-1.2-3.4-1.2-.4-1.1-1.1-1.4-1.1-1.4-.9-.6.1-.6.1-.6 1 .1 1.6 1 1.6 1 .9 1.5 2.3 1.1 2.9.8.1-.7.4-1.1.6-1.3-2.2-.3-4.6-1.1-4.6-5a4 4 0 0 1 1-2.8c-.1-.3-.4-1.3.1-2.7 0 0 .8-.2 2.8 1.1a9.6 9.6 0 0 1 5 0c1.9-1.3 2.8-1.1 2.8-1.1.5 1.4.2 2.4.1 2.7a4 4 0 0 1 1 2.8c0 3.9-2.4 4.8-4.6 5 .4.3.7.9.7 1.8v2.8c0 .3.2.6.7.5A10.1 10.1 0 0 0 12 1.9Z",
};

export function Icon({ name }: { name: keyof typeof paths }) {
  return (
    <svg className="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none">
      {name === "github"
        ? <path d={paths[name]} fill="currentColor" />
        : <path d={paths[name]} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />}
    </svg>
  );
}
