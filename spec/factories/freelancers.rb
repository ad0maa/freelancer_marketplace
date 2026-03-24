FactoryBot.define do
  factory :freelancer do
    name { Faker::Name.name }
    email { Faker::Internet.email }
    hourly_rate { Faker::Number.decimal(l_digits: 2, r_digits: 2) }
    availability { true }
  end
end
