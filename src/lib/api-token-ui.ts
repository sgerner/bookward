export function tokenForView(view: string, token: string | null) {
  return view === "settings" ? token : null;
}
