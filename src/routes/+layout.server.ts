import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = ({ locals }) => {
	if (!locals.user) return { user: null };
	const hasLocalUsername =
		locals.user.has_local_username ??
		(Boolean(locals.user.username.trim()) && !locals.user.username.trim().toLowerCase().startsWith('oidc:'));
	return {
		user: {
			...locals.user,
			username: hasLocalUsername ? locals.user.username : '',
			has_local_username: hasLocalUsername
		}
	};
};
