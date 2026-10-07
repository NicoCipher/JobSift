export type OperatorProfileCatalogueEntry = {
  profile_id: string;
  client_name: string;
  destination_name: string;
};

export function parseOperatorProfiles(
  raw: string | null | undefined,
): OperatorProfileCatalogueEntry[] {
  const source = raw?.trim();
  if (!source) return [];
  try {
    const parsed = JSON.parse(source) as unknown;
    if (!Array.isArray(parsed)) return [];
    const seen = new Set<string>();
    return parsed.flatMap((value): OperatorProfileCatalogueEntry[] => {
      if (!value || typeof value !== "object") return [];
      const item = value as Record<string, unknown>;
      const profileId =
        typeof item.profile_id === "string"
          ? item.profile_id.trim().toLowerCase()
          : "";
      const clientName =
        typeof item.client_name === "string"
          ? item.client_name.trim().slice(0, 120)
          : "";
      const destinationName =
        typeof item.destination_name === "string"
          ? item.destination_name.trim().slice(0, 120)
          : "";
      if (
        !/^[0-9a-f]{16}$/.test(profileId) ||
        !clientName ||
        !destinationName ||
        seen.has(profileId)
      ) {
        return [];
      }
      seen.add(profileId);
      return [
        {
          profile_id: profileId,
          client_name: clientName,
          destination_name: destinationName,
        },
      ];
    });
  } catch {
    return [];
  }
}

export function isOperatorProfileAllowed(
  raw: string | null | undefined,
  profileId: string,
): boolean {
  const source = raw?.trim();
  if (!source) return false;
  return parseOperatorProfiles(source).some(
    (profile) => profile.profile_id === profileId.trim().toLowerCase(),
  );
}
