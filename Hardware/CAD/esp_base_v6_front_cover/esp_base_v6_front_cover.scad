// Removable front cover for esp_base_v6.
//
// Assembly coordinates match esp_base_v6-Body.stl:
//   x = enclosure length, y = enclosure width, z = height.
// The plate occupies the existing 3 mm ledge at x=63..66 and z=3..24.
// Two cantilever fingers grip the lower inner side walls without entering the
// perfboard rail zone, whose lower edge is at z=18.25 mm.

$fn = 128;

base_front_x = 66;
wall_front_x = 63;
base_top_z = 3;
enclosure_top_z = 24;
enclosure_y_min = -46;
enclosure_y_max = 0;

hole_diameter = 8;
hole_center_y = -35.85;
hole_center_z = 9;

clip_x_tip = 60;
clip_x_body_start = 60.3;
clip_x_root = 63.5;
clip_z_min = 15.5;
clip_z_max = 18;
clip_bump_z_min = 16;
clip_bump_z_max = 17.5;
clip_body_thickness = 1.2;
clip_body_clearance = 0.30;
clip_interference = 0.10;

left_inner_wall_y = -43;
left_outer_body_y = left_inner_wall_y + clip_body_clearance;
left_inner_body_y = left_outer_body_y + clip_body_thickness;

module vertical_prism(points, z_min, z_max) {
    translate([0, 0, z_min])
        linear_extrude(height = z_max - z_min)
            polygon(points = points);
}

module pierced_plate() {
    difference() {
        translate([wall_front_x, enclosure_y_min, base_top_z])
            cube([
                base_front_x - wall_front_x,
                enclosure_y_max - enclosure_y_min,
                enclosure_top_z - base_top_z
            ]);
        translate([wall_front_x - 0.5, hole_center_y, hole_center_z])
            rotate([0, 90, 0])
                cylinder(h = base_front_x - wall_front_x + 1,
                         d = hole_diameter);
    }
}

module left_clip() {
    translate([clip_x_body_start, left_outer_body_y, clip_z_min])
        cube([
            clip_x_root - clip_x_body_start,
            clip_body_thickness,
            clip_z_max - clip_z_min
        ]);

    vertical_prism([
        [clip_x_tip, (left_outer_body_y + left_inner_body_y) / 2],
        [clip_x_body_start, left_outer_body_y],
        [clip_x_body_start, left_inner_body_y]
    ], clip_z_min, clip_z_max);

    vertical_prism([
        [60.3, left_outer_body_y],
        [61.14, left_outer_body_y],
        [60.9, left_inner_wall_y - clip_interference],
        [60.54, left_inner_wall_y - clip_interference]
    ], clip_bump_z_min, clip_bump_z_max);
}

module right_clip() {
    mirror([0, 1, 0])
        translate([0, 46, 0])
            left_clip();
}

module unnotched_cover() {
    union() {
        pierced_plate();
        left_clip();
        right_clip();
    }
}

module print_ready_cover() {
    // Assembly (x,y,z) -> print (-y, z-3, 66-x), putting the visible face
    // flat on the build plate and the flexible fingers upward.
    multmatrix([
        [ 0, -1,  0,  0],
        [ 0,  0,  1, -3],
        [-1,  0,  0, 66],
        [ 0,  0,  0,  1]
    ]) assembled_cover();
}

print_ready_cover();

// Top notch: 11 mm wide, 2 mm deep; 9.5 mm from outside-view right edge.
module assembled_cover() {
    difference() {
        unnotched_cover();
        translate([62.9, -20.5, 22]) cube([3.2, 11, 2.1]);
    }
}
