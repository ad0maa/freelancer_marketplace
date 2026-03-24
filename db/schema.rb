# This file is auto-generated from the current state of the database. Instead
# of editing this file, please use the migrations feature of Active Record to
# incrementally modify your database, and then regenerate this schema definition.
#
# This file is the source Rails uses to define your schema when running `bin/rails
# db:schema:load`. When creating a new database, `bin/rails db:schema:load` tends to
# be faster and is potentially less error prone than running all of your
# migrations from scratch. Old migrations may fail to apply correctly if those
# migrations use external dependencies or application code.
#
# It's strongly recommended that you check this file into your version control system.

ActiveRecord::Schema[8.1].define(version: 2026_03_24_003954) do
  create_table "bookings", force: :cascade do |t|
    t.integer "client_id", null: false
    t.datetime "created_at", null: false
    t.date "end_date", null: false
    t.integer "freelancer_id", null: false
    t.date "start_date", null: false
    t.integer "status", default: 0, null: false
    t.decimal "total_amount", precision: 10, scale: 2, null: false
    t.datetime "updated_at", null: false
    t.index ["client_id"], name: "index_bookings_on_client_id"
    t.index ["freelancer_id"], name: "index_bookings_on_freelancer_id"
  end

  create_table "clients", force: :cascade do |t|
    t.datetime "created_at", null: false
    t.string "email", null: false
    t.string "name", null: false
    t.datetime "updated_at", null: false
    t.index ["email"], name: "index_clients_on_email", unique: true
  end

  create_table "freelancer_skills", force: :cascade do |t|
    t.datetime "created_at", null: false
    t.integer "freelancer_id", null: false
    t.integer "skill_id", null: false
    t.datetime "updated_at", null: false
    t.index ["freelancer_id", "skill_id"], name: "index_freelancer_skills_on_freelancer_id_and_skill_id", unique: true
    t.index ["freelancer_id"], name: "index_freelancer_skills_on_freelancer_id"
    t.index ["skill_id"], name: "index_freelancer_skills_on_skill_id"
  end

  create_table "freelancers", force: :cascade do |t|
    t.boolean "availability", default: true
    t.datetime "created_at", null: false
    t.string "email", null: false
    t.decimal "hourly_rate", precision: 8, scale: 2, default: "0.0"
    t.string "name", null: false
    t.datetime "updated_at", null: false
    t.index ["email"], name: "index_freelancers_on_email", unique: true
  end

  create_table "skills", force: :cascade do |t|
    t.datetime "created_at", null: false
    t.string "name", null: false
    t.datetime "updated_at", null: false
    t.index ["name"], name: "index_skills_on_name", unique: true
  end

  add_foreign_key "bookings", "clients"
  add_foreign_key "bookings", "freelancers"
  add_foreign_key "freelancer_skills", "freelancers"
  add_foreign_key "freelancer_skills", "skills"
end
