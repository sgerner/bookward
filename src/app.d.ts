// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare module '*?raw' {
	const content: string;
	export default content;
}

declare global {
		namespace App {
		// interface Error {}
		interface Locals {
			user?: { id: string; profile_id: string; username: string; display_name: string; role: 'admin' | 'user'; must_change_password: boolean };
		}
		// interface PageData {}
		// interface PageState {}
		// interface Platform {}
	}
}

export {};
