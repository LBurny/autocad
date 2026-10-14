using System.Reflection;
using System.Runtime.InteropServices;

namespace AcadMcp.ApiProbe;

/// <summary>
/// Prints the signature of AutoCAD API members by reading the assemblies as
/// metadata only.
/// </summary>
/// <remarks>
/// Usage:
///   AcadMcp.ApiProbe &lt;acadDir&gt; &lt;assembly&gt; &lt;TypeName&gt; [member filter]
///
/// Example:
///   AcadMcp.ApiProbe "F:\AutoCAD\AutoCAD 2026" acdbmgd Autodesk.AutoCAD.DatabaseServices.Hatch Append
///
/// MetadataLoadContext never executes the assembly, so this works with AutoCAD
/// closed — which is the whole point, since a loaded plugin assembly cannot be
/// unloaded and every API mistake otherwise costs an AutoCAD restart.
/// </remarks>
internal static class Program
{
    private static int Main(string[] args)
    {
        if (args.Length < 3)
        {
            Console.Error.WriteLine(
                "usage: AcadMcp.ApiProbe <acadDir> <assembly> <TypeName> [member filter]");
            return 2;
        }

        var acadDir = args[0];
        var assemblyName = args[1];
        var typeName = args[2];
        var filter = args.Length > 3 ? args[3] : null;

        var assemblyPath = Path.Combine(acadDir, assemblyName + ".dll");
        if (!File.Exists(assemblyPath))
        {
            Console.Error.WriteLine($"not found: {assemblyPath}");
            return 2;
        }

        var paths = Directory.GetFiles(acadDir, "*.dll").ToList();
        paths.AddRange(Directory.GetFiles(RuntimeEnvironment.GetRuntimeDirectory(), "*.dll"));
        var resolver = new PathAssemblyResolver(paths);

        using var context = new MetadataLoadContext(resolver);
        Assembly assembly;
        try
        {
            assembly = context.LoadFromAssemblyPath(assemblyPath);
        }
        catch (Exception error)
        {
            Console.Error.WriteLine($"could not load {assemblyPath}: {error.Message}");
            return 1;
        }

        var type = assembly.GetType(typeName)
            ?? assembly.GetTypes().FirstOrDefault(t => t.FullName == typeName || t.Name == typeName);
        if (type is null)
        {
            Console.Error.WriteLine($"type not found: {typeName}");
            return 1;
        }

        Console.WriteLine($"=== {type.FullName}  (base: {SafeName(type.BaseType)}) ===");

        foreach (var line in Members(type, filter))
        {
            Console.WriteLine(line);
        }
        return 0;
    }

    private static IEnumerable<string> Members(Type type, string? filter)
    {
        const BindingFlags flags = BindingFlags.Public | BindingFlags.Instance
            | BindingFlags.Static | BindingFlags.DeclaredOnly;

        foreach (var property in type.GetProperties(flags).OrderBy(p => p.Name))
        {
            if (!Matches(property.Name, filter))
            {
                continue;
            }
            var accessors = (property.CanRead ? "get " : "")
                + (property.CanWrite ? "set" : "");
            yield return $"  prop {SafeName(property.PropertyType)} {property.Name} [{accessors.Trim()}]";
        }

        foreach (var field in type.GetFields(flags).OrderBy(f => f.Name))
        {
            if (Matches(field.Name, filter))
            {
                yield return $"  field {SafeName(field.FieldType)} {field.Name}";
            }
        }

        foreach (var evt in type.GetEvents(flags).OrderBy(e => e.Name))
        {
            if (Matches(evt.Name, filter))
            {
                yield return $"  event {SafeName(evt.EventHandlerType)} {evt.Name}";
            }
        }

        foreach (var ctor in type.GetConstructors(flags).OrderBy(c => c.GetParameters().Length))
        {
            var ctorParams = string.Join(", ", ctor.GetParameters()
                .Select(p => $"{SafeName(p.ParameterType)} {p.Name}"));
            yield return $"  new {type.Name}({ctorParams})";
        }

        foreach (var method in type.GetMethods(flags)
                     .Where(m => !m.IsSpecialName)
                     .OrderBy(m => m.Name))
        {
            if (!Matches(method.Name, filter))
            {
                continue;
            }
            var parameters = string.Join(", ", method.GetParameters()
                .Select(p => $"{SafeName(p.ParameterType)} {p.Name}"
                    + (p.HasDefaultValue ? " = ..." : "")));
            yield return $"  {SafeName(method.ReturnType)} {method.Name}({parameters})";
        }
    }

    private static bool Matches(string name, string? filter) =>
        filter is null || name.Contains(filter, StringComparison.OrdinalIgnoreCase);

    private static string SafeName(Type? type)
    {
        if (type is null)
        {
            return "?";
        }
        if (type.IsGenericType)
        {
            var stem = type.Name.Split('`')[0];
            var args = string.Join(", ", type.GetGenericArguments().Select(SafeName));
            return $"{stem}<{args}>";
        }
        return type.Name;
    }
}
